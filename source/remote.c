// SPDX-License-Identifier: GPL-2.0
/* HTTP-backed VFS operations; dirty pages stay local until acknowledged. */
#include <linux/fs.h>
#include <linux/fs_context.h>
#include <linux/highmem.h>
#include <linux/list.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/pagemap.h>
#include <linux/slab.h>
#include <linux/uio.h>
#include <linux/vmalloc.h>
#include <linux/workqueue.h>
#include "http.h"

#define GOI_READONLY 1
#define GOI_BRANCH 2
#define GOI_SYNC_ALL 0x4701
#define GOI_SWITCH 0x4702

struct goi_meta {
	__le64 id;
	__le64 size;
	__le64 mtime;
	__le32 mode;
	__le32 flags;
	__le64 revision;
} __packed;

struct goi_super {
	struct super_block *sb;
	struct vtfs_remote_config config;
	struct mutex tracked_lock;
	struct list_head tracked;
	struct delayed_work sync_work;
	u64 active_branch;
	atomic64_t cache_used;
};

struct goi_inode {
	struct inode *inode;
	struct list_head tracked;
	u64 revision;
	u32 flags;
	u64 branch;
	unsigned int opens;
	bool loaded;
	bool dirty;
	u64 reserved;
};

static const struct inode_operations goi_dir_inode_ops;
static const struct inode_operations goi_file_inode_ops;
static const struct file_operations goi_dir_ops;
static const struct file_operations goi_file_ops;
static const struct dentry_operations goi_dentry_ops;

static int goi_call(struct super_block *sb, const char *method, const char *query,
		    const void *body, size_t length, void *reply, size_t capacity,
		    size_t *received)
{
	struct goi_super *info = sb->s_fs_info;

	return vtfs_http_call(&info->config, method, query, body, length,
			      reply, capacity, received);
}

static int goi_stat(struct inode *inode, struct goi_meta *meta)
{
	char query[64];
	size_t received;
	int ret;

	snprintf(query, sizeof(query), "id=%lu", inode->i_ino);
	ret = goi_call(inode->i_sb, "stat", query, NULL, 0, meta, sizeof(*meta), &received);
	if (!ret && received != sizeof(*meta))
		ret = -EPROTO;
	return ret;
}

static void goi_apply_meta(struct inode *inode, const struct goi_meta *meta)
{
	struct goi_inode *info = inode->i_private;
	u64 ns = le64_to_cpu(meta->mtime);
	struct timespec64 stamp = ns_to_timespec64(ns);

	inode->i_mode = le32_to_cpu(meta->mode);
	if (le32_to_cpu(meta->flags) & GOI_READONLY)
		inode->i_mode &= ~0222;
	i_size_write(inode, le64_to_cpu(meta->size));
	inode_set_mtime_to_ts(inode, stamp);
	inode_set_ctime_to_ts(inode, stamp);
	inode_set_atime_to_ts(inode, stamp);
	info->revision = le64_to_cpu(meta->revision);
	info->flags = le32_to_cpu(meta->flags);
}

static struct inode *goi_new_inode(struct super_block *sb, const struct goi_meta *meta, u64 branch)
{
	struct goi_super *super = sb->s_fs_info;
	struct goi_inode *info;
	struct inode *inode;
	umode_t mode = le32_to_cpu(meta->mode);

	if ((!S_ISREG(mode) && !S_ISDIR(mode)) ||
	    le64_to_cpu(meta->size) > VTFS_MAX_FILE_SIZE)
		return ERR_PTR(-EFBIG);
	info = kzalloc_obj(*info, GFP_KERNEL);
	if (!info)
		return ERR_PTR(-ENOMEM);
	inode = new_inode(sb);
	if (!inode) {
		kfree(info);
		return ERR_PTR(-ENOMEM);
	}
	inode->i_ino = le64_to_cpu(meta->id);
	inode->i_uid = make_kuid(&init_user_ns, super->config.uid);
	inode->i_gid = make_kgid(&init_user_ns, super->config.gid);
	inode->i_private = info;
	info->inode = inode;
	INIT_LIST_HEAD(&info->tracked);
	goi_apply_meta(inode, meta);
	info->branch = info->flags & GOI_BRANCH ? inode->i_ino : branch;
	if (S_ISDIR(mode)) {
		inode->i_op = &goi_dir_inode_ops;
		inode->i_fop = &goi_dir_ops;
		set_nlink(inode, 2);
	} else {
		inode->i_op = &goi_file_inode_ops;
		inode->i_fop = &goi_file_ops;
		inode->i_mapping->a_ops = &ram_aops;
		mapping_set_gfp_mask(inode->i_mapping, GFP_HIGHUSER);
		mapping_set_unevictable(inode->i_mapping);
	}
	mutex_lock(&super->tracked_lock);
	list_add_tail(&info->tracked, &super->tracked);
	mutex_unlock(&super->tracked_lock);
	return inode;
}

/* Reservations include holes and dirty/open files, so the limit is conservative. */
static void goi_resize_reservation(struct inode *inode, u64 bytes)
{
	struct goi_super *super = inode->i_sb->s_fs_info;
	struct goi_inode *info = inode->i_private;

	atomic64_add((s64)bytes - info->reserved, &super->cache_used);
	info->reserved = bytes;
}

static void goi_discard_cache(struct inode *inode)
{
	struct goi_inode *info = inode->i_private;

	truncate_inode_pages(inode->i_mapping, 0);
	info->loaded = false;
	goi_resize_reservation(inode, 0);
}

static int goi_reserve(struct inode *inode, loff_t size)
{
	struct goi_super *super = inode->i_sb->s_fs_info;
	struct goi_inode *info = inode->i_private, *other;
	u64 bytes = PAGE_ALIGN(size), limit = (u64)super->config.cache_mib << 20;
	s64 extra = bytes - info->reserved;

	if (extra <= 0)
		return 0;
	if (bytes > limit)
		return -ENOSPC;
	/* trylock avoids ABBA with simultaneous opens/writes in another inode. */
	mutex_lock(&super->tracked_lock);
	list_for_each_entry(other, &super->tracked, tracked) {
		struct inode *candidate = other->inode;

		if (atomic64_read(&super->cache_used) + extra <= limit)
			break;
		if (candidate == inode || !S_ISREG(candidate->i_mode) || !inode_trylock(candidate))
			continue;
		if (other->loaded && !other->dirty && !other->opens && !mapping_mapped(candidate->i_mapping))
			goi_discard_cache(candidate);
		inode_unlock(candidate);
	}
	if (atomic64_add_return(extra, &super->cache_used) > limit) {
		atomic64_sub(extra, &super->cache_used);
		mutex_unlock(&super->tracked_lock);
		return -ENOSPC;
	}
	info->reserved = bytes;
	mutex_unlock(&super->tracked_lock);
	return 0;
}

/* Caller holds i_rwsem exclusively. Fetch bytes only when a file is opened. */
static int goi_load(struct inode *inode)
{
	struct goi_inode *info = inode->i_private;
	loff_t size = i_size_read(inode), offset = 0;
	char *buffer;
	int ret = 0;

	if (info->loaded)
		return 0;
	if (size > VTFS_MAX_FILE_SIZE)
		return -EFBIG;
	ret = goi_reserve(inode, size);
	if (ret)
		return ret;
	buffer = kvmalloc(VTFS_CHUNK_SIZE, GFP_KERNEL);
	if (!buffer) {
		goi_resize_reservation(inode, 0);
		return -ENOMEM;
	}
	while (offset < size) {
		char query[160];
		size_t count = min_t(loff_t, VTFS_CHUNK_SIZE, size - offset);
		size_t received, copied = 0;

		snprintf(query, sizeof(query), "id=%lu&offset=%lld&count=%zu",
			 inode->i_ino, offset, count);
		ret = goi_call(inode->i_sb, "read", query, NULL, 0, buffer, count, &received);
		if (ret || received != count) {
			ret = ret ? ret : -EPROTO;
			break;
		}
		while (copied < count) {
			loff_t position = offset + copied;
			struct folio *folio = filemap_grab_folio(inode->i_mapping, position >> PAGE_SHIFT);
			size_t amount = min_t(size_t, PAGE_SIZE, count - copied);
			void *mapped;

			if (IS_ERR(folio)) {
				ret = PTR_ERR(folio);
				goto out;
			}
			mapped = kmap_local_folio(folio, 0);
			memcpy(mapped, buffer + copied, amount);
			if (amount < PAGE_SIZE)
				memset(mapped + amount, 0, PAGE_SIZE - amount);
			kunmap_local(mapped);
			folio_mark_uptodate(folio);
			folio_mark_dirty(folio);
			folio_unlock(folio);
			folio_put(folio);
			copied += amount;
		}
		offset += count;
	}
	info->loaded = !ret;
out:
	kvfree(buffer);
	if (ret)
		goi_discard_cache(inode);
	return ret;
}

/* Caller holds i_rwsem. Open files also cover mappings that retain a file reference. */
static void goi_drop_cache(struct inode *inode)
{
	struct goi_super *super = inode->i_sb->s_fs_info;
	struct goi_inode *info = inode->i_private;
	u64 active = READ_ONCE(super->active_branch);

	if (S_ISREG(inode->i_mode) && active && info->branch && info->branch != active &&
	    info->loaded && !info->dirty && !info->opens && !mapping_mapped(inode->i_mapping)) {
		goi_discard_cache(inode);
	}
}

/* Snapshot while i_rwsem is held; the same whole-file format is used by fsync. */
static char *goi_snapshot(struct inode *inode)
{
	loff_t size = i_size_read(inode), offset;
	char *buffer = kvzalloc(max_t(size_t, size, 1), GFP_KERNEL);

	if (!buffer)
		return ERR_PTR(-ENOMEM);
	for (offset = 0; offset < size; offset += PAGE_SIZE) {
		struct folio *folio = filemap_get_folio(inode->i_mapping, offset >> PAGE_SHIFT);

		if (!IS_ERR(folio)) {
			void *mapped = kmap_local_folio(folio, 0);

			memcpy(buffer + offset, mapped, min_t(loff_t, PAGE_SIZE, size - offset));
			kunmap_local(mapped);
			folio_put(folio);
		}
	}
	return buffer;
}

static int goi_journal(struct inode *inode, bool acknowledge)
{
	struct goi_super *super = inode->i_sb->s_fs_info;
	struct goi_inode *info = inode->i_private;
	struct vtfs_remote_config config;
	char query[128], *buffer = NULL;
	size_t received;
	int ret;

	if (!super->config.journal_port)
		return 0;
	config = super->config;
	strscpy(config.server, "127.0.0.1", sizeof(config.server));
	config.port = config.journal_port;
	snprintf(query, sizeof(query), "id=%lu&revision=%llu&mode=%u",
		 inode->i_ino, info->revision, inode->i_mode & 0777);
	if (!acknowledge) {
		buffer = goi_snapshot(inode);
		if (IS_ERR(buffer))
			return PTR_ERR(buffer);
	}
	/* A non-NULL body selects POST, including for empty files and acknowledgments. */
	ret = vtfs_http_call(&config, acknowledge ? "ack" : "record", query,
			    acknowledge ? "" : buffer, acknowledge ? 0 : i_size_read(inode),
			    NULL, 0, &received);
	kvfree(buffer);
	if (!ret && received)
		ret = -EPROTO;
	return ret;
}

/* fsync: send the complete local file, then wait for the server's SQLite commit. */
static int goi_sync_inode(struct inode *inode)
{
	struct goi_inode *info = inode->i_private;
	struct goi_meta meta;
	char query[96], *buffer;
	loff_t size;
	size_t received;
	int ret = 0;

	inode_lock(inode);
	if (!info->dirty || !S_ISREG(inode->i_mode))
		goto unlock;
	size = i_size_read(inode);
	buffer = goi_snapshot(inode);
	if (IS_ERR(buffer)) {
		ret = PTR_ERR(buffer);
		goto unlock;
	}
	snprintf(query, sizeof(query), "id=%lu&mode=%u", inode->i_ino, inode->i_mode & 0777);
	ret = goi_call(inode->i_sb, "write", query, buffer, size, &meta, sizeof(meta), &received);
	if (!ret && received != sizeof(meta))
		ret = -EPROTO;
	if (!ret)
		ret = goi_journal(inode, true);
	if (!ret) {
		info->revision = le64_to_cpu(meta.revision);
		info->dirty = false;
	}
	kvfree(buffer);
unlock:
	if (!ret)
		goi_drop_cache(inode);
	inode_unlock(inode);
	return ret;
}

/* keep_branch=0 means sync only; switching also drops closed files in other branches. */
static int goi_sync_all(struct super_block *sb, u64 keep_branch)
{
	struct goi_super *super = sb->s_fs_info;
	struct goi_inode *info;
	struct inode **inodes;
	size_t count = 0, used = 0, i;
	int error = 0;

	mutex_lock(&super->tracked_lock);
	list_for_each_entry(info, &super->tracked, tracked)
		count++;
	inodes = kcalloc(count, sizeof(*inodes), GFP_KERNEL);
	if (!inodes) {
		mutex_unlock(&super->tracked_lock);
		return -ENOMEM;
	}
	list_for_each_entry(info, &super->tracked, tracked) {
		struct inode *inode = igrab(info->inode);

		if (inode)
			inodes[used++] = inode;
	}
	mutex_unlock(&super->tracked_lock);
	for (i = 0; i < used; i++) {
		int ret = goi_sync_inode(inodes[i]);

		if (ret && !error)
			error = ret;
	}
	if (!error && keep_branch) {
		WRITE_ONCE(super->active_branch, keep_branch);
		for (i = 0; i < used; i++) {
			inode_lock(inodes[i]);
			goi_drop_cache(inodes[i]);
			inode_unlock(inodes[i]);
		}
	}
	for (i = 0; i < used; i++)
		iput(inodes[i]);
	kfree(inodes);
	return error;
}

static void goi_background_sync(struct work_struct *work)
{
	struct goi_super *super = container_of(to_delayed_work(work), struct goi_super, sync_work);
	int ret = goi_sync_all(super->sb, 0);

	if (ret)
		pr_warn_ratelimited("[vtfs] background sync failed: %d; local data retained\n", ret);
	queue_delayed_work(system_long_wq, &super->sync_work, super->config.autosync * HZ);
}

static int goi_open(struct inode *inode, struct file *file)
{
	struct goi_inode *info = inode->i_private;
	int ret = 0;

	inode_lock(inode);
	if ((file->f_flags & O_TRUNC) && !(info->flags & GOI_READONLY))
		info->loaded = true;
	else
		ret = goi_load(inode);
	if (!ret)
		info->opens++;
	inode_unlock(inode);
	return ret;
}

static int goi_release(struct inode *inode, struct file *file)
{
	struct goi_inode *info = inode->i_private;

	inode_lock(inode);
	info->opens--;
	goi_drop_cache(inode);
	inode_unlock(inode);
	return 0;
}

static ssize_t goi_write_iter(struct kiocb *iocb, struct iov_iter *from)
{
	struct inode *inode = file_inode(iocb->ki_filp);
	struct goi_inode *info = inode->i_private;
	ssize_t ret;

	inode_lock(inode);
	ret = goi_load(inode);
	if (ret)
		goto out;
	ret = generic_write_checks(iocb, from);
	if (ret > 0) {
		ret = goi_reserve(inode, max_t(loff_t, i_size_read(inode), iocb->ki_pos + iov_iter_count(from)));
		if (ret)
			goto out;
		ret = __generic_file_write_iter(iocb, from);
		if (ret > 0) {
			int saved;

			info->dirty = true;
			saved = goi_journal(inode, false);
			if (saved)
				ret = saved;
		}
	}
out:
	inode_unlock(inode);
	return ret > 0 ? generic_write_sync(iocb, ret) : ret;
}

static int goi_fsync(struct file *file, loff_t start, loff_t end, int datasync)
{
	return goi_sync_inode(file_inode(file));
}

static int goi_mmap_prepare(struct vm_area_desc *desc)
{
	if (vma_flags_test(&desc->vma_flags, VMA_SHARED_BIT)) {
		if (vma_flags_test(&desc->vma_flags, VMA_WRITE_BIT))
			return -EINVAL;
		/* A read mapping on an O_RDWR fd must never be upgraded by mprotect. */
		vma_flags_clear(&desc->vma_flags, VMA_MAYWRITE_BIT);
	}
	return generic_file_mmap_prepare(desc);
}

static long goi_ioctl(struct file *file, unsigned int command, unsigned long arg)
{
	struct inode *inode = file_inode(file);
	struct goi_inode *info = inode->i_private;
	struct goi_super *super = inode->i_sb->s_fs_info;

	if (command != GOI_SYNC_ALL && command != GOI_SWITCH)
		return -ENOTTY;
	if (!inode_owner_or_capable(file_mnt_idmap(file), inode))
		return -EACCES;
	if (command == GOI_SWITCH) {
		if (!(info->flags & GOI_BRANCH))
			return -EINVAL;
		if (READ_ONCE(super->active_branch) == info->branch)
			return 0;
		return goi_sync_all(inode->i_sb, info->branch);
	}
	return goi_sync_all(inode->i_sb, 0);
}

static int goi_setattr(struct mnt_idmap *idmap, struct dentry *dentry, struct iattr *attr)
{
	struct inode *inode = d_inode(dentry);
	struct goi_inode *info = inode->i_private;
	int ret;

	if (info->flags & GOI_READONLY)
		return -EACCES;
	if (S_ISDIR(inode->i_mode)) {
		struct goi_meta meta;
		char query[96];
		size_t received;

		if (attr->ia_valid & ATTR_SIZE)
			return -EISDIR;
		snprintf(query, sizeof(query), "id=%lu&mode=%u", inode->i_ino,
			 (attr->ia_valid & ATTR_MODE ? attr->ia_mode : inode->i_mode) & 0777);
		ret = goi_call(inode->i_sb, "setattr", query, NULL, 0, &meta, sizeof(meta), &received);
		if (ret)
			return ret;
		if (received != sizeof(meta))
			return -EPROTO;
		info->revision = le64_to_cpu(meta.revision);
	} else if (!(attr->ia_valid & ATTR_SIZE) || attr->ia_size != 0) {
		/* chmod/touch must not turn an unfetched file into zero-filled data. */
		ret = goi_load(inode);
		if (ret)
			return ret;
	}
	if (S_ISREG(inode->i_mode) && (attr->ia_valid & ATTR_SIZE)) {
		ret = goi_reserve(inode, attr->ia_size);
		if (ret)
			return ret;
	}
	ret = simple_setattr(idmap, dentry, attr);
	if (!ret && S_ISREG(inode->i_mode)) {
		goi_resize_reservation(inode, PAGE_ALIGN(i_size_read(inode)));
		info->loaded = true;
		info->dirty = true;
		ret = goi_journal(inode, false);
	}
	return ret;
}

static struct dentry *goi_lookup(struct inode *dir, struct dentry *dentry, unsigned int flags)
{
	struct goi_meta meta;
	struct inode *inode;
	char *name = vtfs_url_encode(dentry->d_name.name), *query;
	size_t received;
	int ret;

	if (IS_ERR(name))
		return ERR_CAST(name);
	query = kasprintf(GFP_KERNEL, "parent=%lu&name=%s", dir->i_ino, name);
	kfree(name);
	if (!query)
		return ERR_PTR(-ENOMEM);
	ret = goi_call(dir->i_sb, "lookup", query, NULL, 0, &meta, sizeof(meta), &received);
	kfree(query);
	if (ret == -ENOENT) {
		d_add(dentry, NULL);
		return NULL;
	}
	if (ret)
		return ERR_PTR(ret);
	if (received != sizeof(meta))
		return ERR_PTR(-EPROTO);
	inode = goi_new_inode(dir->i_sb, &meta, ((struct goi_inode *)dir->i_private)->branch);
	if (IS_ERR(inode))
		return ERR_CAST(inode);
	/* d_add completes parallel lookup before d_u becomes the alias list. */
	d_add(dentry, inode);
	spin_lock(&dentry->d_lock);
	dentry->d_flags |= DCACHE_PERSISTENT;
	dget_dlock(dentry);
	spin_unlock(&dentry->d_lock);
	return NULL;
}

static int goi_revalidate(struct inode *dir, const struct qstr *name,
			  struct dentry *dentry, unsigned int flags)
{
	struct inode *inode = d_inode(dentry);
	struct goi_inode *info;
	struct goi_meta meta;
	int ret;

	if (flags & LOOKUP_RCU)
		return -ECHILD;
	if (!inode)
		return 0;
	info = inode->i_private;
	if (READ_ONCE(info->dirty))
		return 1;
	ret = goi_stat(inode, &meta);
	if (ret == -ENOENT)
		return 0;
	if (ret)
		return ret;
	if (!S_ISDIR(inode->i_mode) && le64_to_cpu(meta.revision) != info->revision)
		return 0;
	return 1;
}

static int goi_iterate(struct file *file, struct dir_context *ctx)
{
	struct inode *inode = file_inode(file);
	char *buffer;
	int ret = 0;

	if (!dir_emit_dots(file, ctx))
		return 0;
	buffer = kvmalloc(VTFS_CHUNK_SIZE, GFP_KERNEL);
	if (!buffer)
		return -ENOMEM;
	for (;;) {
		char query[96];
		size_t received, cursor = sizeof(__le32);
		__le32 wire_count;
		u32 count, i;

		snprintf(query, sizeof(query), "id=%lu&offset=%lld", inode->i_ino, ctx->pos - 2);
		ret = goi_call(inode->i_sb, "list", query, NULL, 0, buffer, VTFS_CHUNK_SIZE, &received);
		if (ret)
			break;
		if (received < sizeof(wire_count)) {
			ret = -EPROTO;
			break;
		}
		memcpy(&wire_count, buffer, sizeof(wire_count));
		count = le32_to_cpu(wire_count);
		if (!count)
			break;
		for (i = 0; i < count; i++) {
			struct goi_meta meta;
			__le16 wire_length;
			u16 length;

			if (received - cursor < sizeof(meta) + sizeof(wire_length)) {
				ret = -EPROTO;
				goto out;
			}
			memcpy(&meta, buffer + cursor, sizeof(meta));
			cursor += sizeof(meta);
			memcpy(&wire_length, buffer + cursor, sizeof(wire_length));
			cursor += sizeof(wire_length);
			length = le16_to_cpu(wire_length);
			if (!length || length > NAME_MAX || received - cursor < length) {
				ret = -EPROTO;
				goto out;
			}
			if (!dir_emit(ctx, buffer + cursor, length, le64_to_cpu(meta.id),
				      fs_umode_to_dtype(le32_to_cpu(meta.mode))))
				goto out;
			ctx->pos++;
			cursor += length;
		}
	}
out:
	kvfree(buffer);
	return ret;
}

static int goi_create_common(struct inode *dir, struct dentry *dentry, umode_t mode)
{
	struct goi_meta meta;
	struct inode *inode;
	char *name = vtfs_url_encode(dentry->d_name.name), *query;
	size_t received;
	int ret;

	if (IS_ERR(name))
		return PTR_ERR(name);
	query = kasprintf(GFP_KERNEL, "parent=%lu&name=%s&mode=%u", dir->i_ino, name, mode);
	kfree(name);
	if (!query)
		return -ENOMEM;
	ret = goi_call(dir->i_sb, "create", query, NULL, 0, &meta, sizeof(meta), &received);
	kfree(query);
	if (ret)
		return ret;
	if (received != sizeof(meta))
		return -EPROTO;
	inode = goi_new_inode(dir->i_sb, &meta, ((struct goi_inode *)dir->i_private)->branch);
	if (IS_ERR(inode))
		return PTR_ERR(inode);
	((struct goi_inode *)inode->i_private)->loaded = true;
	d_make_persistent(dentry, inode);
	inode_set_mtime_to_ts(dir, inode_set_ctime_current(dir));
	return 0;
}

static int goi_create(struct mnt_idmap *idmap, struct inode *dir,
		      struct dentry *dentry, umode_t mode, bool excl)
{
	return goi_create_common(dir, dentry, S_IFREG | mode);
}

static struct dentry *goi_mkdir(struct mnt_idmap *idmap, struct inode *dir,
				struct dentry *dentry, umode_t mode)
{
	int ret = goi_create_common(dir, dentry, S_IFDIR | mode);

	if (ret)
		return ERR_PTR(ret);
	inc_nlink(dir);
	return NULL;
}

static int goi_remove(struct inode *dir, struct dentry *dentry, bool directory)
{
	char *name = vtfs_url_encode(dentry->d_name.name), *query;
	size_t received;
	int ret;

	if (IS_ERR(name))
		return PTR_ERR(name);
	query = kasprintf(GFP_KERNEL, "parent=%lu&name=%s", dir->i_ino, name);
	kfree(name);
	if (!query)
		return -ENOMEM;
	ret = goi_call(dir->i_sb, directory ? "rmdir" : "unlink", query, NULL, 0, NULL, 0, &received);
	kfree(query);
	if (!ret) {
		struct goi_inode *info = d_inode(dentry)->i_private;

		info->revision++;
		ret = directory ? simple_rmdir(dir, dentry) : simple_unlink(dir, dentry);
	}
	return ret;
}

static int goi_unlink(struct inode *dir, struct dentry *dentry)
{
	return goi_remove(dir, dentry, false);
}

static int goi_rmdir(struct inode *dir, struct dentry *dentry)
{
	return goi_remove(dir, dentry, true);
}

static int goi_rename(struct mnt_idmap *idmap, struct inode *old_dir,
		      struct dentry *old_dentry, struct inode *new_dir,
		      struct dentry *new_dentry, unsigned int flags)
{
	struct goi_meta meta;
	char *old_name = vtfs_url_encode(old_dentry->d_name.name);
	char *new_name = vtfs_url_encode(new_dentry->d_name.name), *query;
	size_t received;
	int ret;

	if (IS_ERR(old_name) || IS_ERR(new_name)) {
		ret = IS_ERR(old_name) ? PTR_ERR(old_name) : PTR_ERR(new_name);
		goto free_names;
	}
	query = kasprintf(GFP_KERNEL, "old_parent=%lu&old_name=%s&new_parent=%lu&new_name=%s&flags=%u",
			  old_dir->i_ino, old_name, new_dir->i_ino, new_name, flags);
	if (!query) {
		ret = -ENOMEM;
		goto free_names;
	}
	ret = goi_call(old_dir->i_sb, "rename", query, NULL, 0, &meta, sizeof(meta), &received);
	kfree(query);
	if (!ret && received != sizeof(meta))
		ret = -EPROTO;
	if (!ret) {
		struct goi_inode *info = d_inode(old_dentry)->i_private;

		if (d_really_is_positive(new_dentry)) {
			struct goi_inode *replaced = d_inode(new_dentry)->i_private;

			replaced->revision++;
		}
		info->revision = le64_to_cpu(meta.revision);
		ret = simple_rename(idmap, old_dir, old_dentry, new_dir, new_dentry, flags);
	}
free_names:
	if (!IS_ERR(old_name))
		kfree(old_name);
	if (!IS_ERR(new_name))
		kfree(new_name);
	return ret;
}

static void goi_evict_inode(struct inode *inode)
{
	struct goi_super *super = inode->i_sb->s_fs_info;
	struct goi_inode *info = inode->i_private;

	if (info) {
		mutex_lock(&super->tracked_lock);
		list_del(&info->tracked);
		mutex_unlock(&super->tracked_lock);
	}
	goi_resize_reservation(inode, 0);
	truncate_inode_pages_final(inode->i_mapping);
	clear_inode(inode);
	kfree(info);
}

static int goi_sync_fs(struct super_block *sb, int wait)
{
	return wait ? goi_sync_all(sb, 0) : 0;
}

static const struct dentry_operations goi_dentry_ops = {
	.d_revalidate = goi_revalidate,
};
static const struct inode_operations goi_file_inode_ops = {
	.getattr = simple_getattr,
	.setattr = goi_setattr,
};
static const struct file_operations goi_file_ops = {
	.owner = THIS_MODULE,
	.open = goi_open,
	.release = goi_release,
	.read_iter = generic_file_read_iter,
	.write_iter = goi_write_iter,
	.llseek = generic_file_llseek,
	.mmap_prepare = goi_mmap_prepare,
	.fsync = goi_fsync,
	.unlocked_ioctl = goi_ioctl,
};
static const struct inode_operations goi_dir_inode_ops = {
	.lookup = goi_lookup,
	.create = goi_create,
	.mkdir = goi_mkdir,
	.unlink = goi_unlink,
	.rmdir = goi_rmdir,
	.rename = goi_rename,
	.getattr = simple_getattr,
	.setattr = goi_setattr,
};
static const struct file_operations goi_dir_ops = {
	.owner = THIS_MODULE,
	.read = generic_read_dir,
	.llseek = generic_file_llseek,
	.iterate_shared = goi_iterate,
	.fsync = goi_fsync,
	.unlocked_ioctl = goi_ioctl,
};
static const struct super_operations goi_super_ops = {
	.statfs = simple_statfs,
	.drop_inode = inode_just_drop,
	.evict_inode = goi_evict_inode,
	.sync_fs = goi_sync_fs,
};

int vtfs_remote_fill_super(struct super_block *sb, struct fs_context *fc)
{
	struct vtfs_remote_config *config = fc->fs_private;
	struct goi_super *super;
	struct goi_meta meta;
	struct inode *root;
	size_t received;
	int ret;

	super = kzalloc_obj(*super, GFP_KERNEL);
	if (!super)
		return -ENOMEM;
	super->config = *config;
	super->sb = sb;
	mutex_init(&super->tracked_lock);
	INIT_LIST_HEAD(&super->tracked);
	INIT_DELAYED_WORK(&super->sync_work, goi_background_sync);
	sb->s_fs_info = super;
	sb->s_op = &goi_super_ops;
	sb->s_magic = 0x676f6901;
	sb->s_maxbytes = VTFS_MAX_FILE_SIZE;
	sb->s_blocksize = PAGE_SIZE;
	sb->s_blocksize_bits = PAGE_SHIFT;
	sb->s_d_flags = DCACHE_DONTCACHE;
	sb->s_time_gran = 1;
	set_default_d_op(sb, &goi_dentry_ops);
	ret = goi_call(sb, "hello", "", NULL, 0, &meta, sizeof(meta), &received);
	if (ret)
		return ret;
	if (received != sizeof(meta) || !S_ISDIR(le32_to_cpu(meta.mode)))
		return -EPROTO;
	root = goi_new_inode(sb, &meta, 0);
	if (IS_ERR(root))
		return PTR_ERR(root);
	/* A GitHub session belongs to the mounting UID, not every local user. */
	root->i_mode = S_IFDIR | 0500;
	sb->s_root = d_make_root(root);
	if (!sb->s_root)
		return -ENOMEM;
	if (config->autosync)
		queue_delayed_work(system_long_wq, &super->sync_work, config->autosync * HZ);
	return 0;
}

void vtfs_remote_kill_sb(struct super_block *sb)
{
	struct goi_super *super = sb->s_fs_info;

	if (super)
		cancel_delayed_work_sync(&super->sync_work);
	kill_anon_super(sb);
	kfree_sensitive(super);
}
