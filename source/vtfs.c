// SPDX-License-Identifier: GPL-2.0
#include <linux/fs.h>
#include <linux/fs_context.h>
#include <linux/fs_parser.h>
#include <linux/inet.h>
#include <linux/init.h>
#include <linux/limits.h>
#include <linux/module.h>
#include <linux/pagemap.h>
#include <linux/printk.h>
#include <linux/security.h>
#include <linux/splice.h>
#include "http.h"

#define MODULE_NAME "vtfs"
#define VTFS_MAGIC 0x676f6901

MODULE_LICENSE("GPL");
MODULE_AUTHOR("secs-dev");
MODULE_DESCRIPTION("GOI filesystem: RAM and HTTP storage backends");
MODULE_ALIAS_FS(MODULE_NAME);

#define LOG(fmt, ...) pr_info("[" MODULE_NAME "]: " fmt, ##__VA_ARGS__)

static const struct inode_operations vtfs_dir_inode_ops;

/* A RAM-only backend cannot acknowledge durable storage on a GOI server. */
static int vtfs_fsync(struct file *file, loff_t start, loff_t end, int datasync)
{
	return -EOPNOTSUPP;
}

static const struct file_operations vtfs_file_ops = {
	.owner = THIS_MODULE,
	.llseek = generic_file_llseek,
	.read_iter = generic_file_read_iter,
	.write_iter = generic_file_write_iter,
	.mmap_prepare = generic_file_mmap_prepare,
	.splice_read = filemap_splice_read,
	.splice_write = iter_file_splice_write,
	.fsync = vtfs_fsync,
};

static const struct inode_operations vtfs_file_inode_ops = {
	.getattr = simple_getattr,
	.setattr = simple_setattr,
};

static const struct file_operations vtfs_dir_ops = {
	.owner = THIS_MODULE,
	.open = dcache_dir_open,
	.release = dcache_dir_close,
	.llseek = dcache_dir_lseek,
	.read = generic_read_dir,
	.iterate_shared = dcache_readdir,
	.fsync = vtfs_fsync,
};

static const struct super_operations vtfs_super_ops = {
	.statfs = simple_statfs,
	.drop_inode = inode_just_drop,
};

static struct inode *vtfs_new_inode(struct mnt_idmap *idmap,
				    struct super_block *sb,
				    const struct inode *parent, umode_t mode)
{
	struct inode *inode = new_inode(sb);

	if (!inode)
		return NULL;

	inode->i_ino = get_next_ino();
	inode_init_owner(idmap, inode, parent, mode);
	simple_inode_init_ts(inode);

	if (S_ISDIR(mode)) {
		inode->i_op = &vtfs_dir_inode_ops;
		inode->i_fop = &vtfs_dir_ops;
		inc_nlink(inode);
	} else {
		inode->i_op = &vtfs_file_inode_ops;
		inode->i_fop = &vtfs_file_ops;
		inode->i_mapping->a_ops = &ram_aops;
		mapping_set_gfp_mask(inode->i_mapping, GFP_HIGHUSER);
		/* RAM is the only copy: clean pages must not be reclaimed. */
		mapping_set_unevictable(inode->i_mapping);
	}

	return inode;
}

static int vtfs_add_inode(struct mnt_idmap *idmap, struct inode *dir,
			  struct dentry *dentry, umode_t mode)
{
	struct inode *inode;
	int ret;

	inode = vtfs_new_inode(idmap, dir->i_sb, dir, mode);
	if (!inode)
		return -ENOMEM;

	ret = security_inode_init_security(inode, dir, &dentry->d_name, NULL, NULL);
	if (ret) {
		iput(inode);
		return ret;
	}

	/* Keep the directory entry alive even after its last user closes it. */
	d_make_persistent(dentry, inode);
	inode_set_mtime_to_ts(dir, inode_set_ctime_current(dir));
	return 0;
}

static int vtfs_create(struct mnt_idmap *idmap, struct inode *dir,
		       struct dentry *dentry, umode_t mode, bool excl)
{
	return vtfs_add_inode(idmap, dir, dentry, S_IFREG | mode);
}

static struct dentry *vtfs_mkdir(struct mnt_idmap *idmap, struct inode *dir,
				 struct dentry *dentry, umode_t mode)
{
	int ret = vtfs_add_inode(idmap, dir, dentry, S_IFDIR | mode);

	if (ret)
		return ERR_PTR(ret);
	inc_nlink(dir);
	return NULL;
}

static const struct inode_operations vtfs_dir_inode_ops = {
	.lookup = simple_lookup,
	.create = vtfs_create,
	.mkdir = vtfs_mkdir,
	.unlink = simple_unlink,
	.rmdir = simple_rmdir,
	.rename = simple_rename,
	.getattr = simple_getattr,
	.setattr = simple_setattr,
};

static int vtfs_fill_super(struct super_block *sb, struct fs_context *fc)
{
	struct inode *root;

	sb->s_magic = VTFS_MAGIC;
	sb->s_op = &vtfs_super_ops;
	/* Unpinned dentries can be destroyed; live RAM entries are persistent. */
	sb->s_d_flags = DCACHE_DONTCACHE;
	sb->s_maxbytes = MAX_LFS_FILESIZE;
	sb->s_blocksize = PAGE_SIZE;
	sb->s_blocksize_bits = PAGE_SHIFT;
	sb->s_time_gran = 1;

	root = vtfs_new_inode(&nop_mnt_idmap, sb, NULL, S_IFDIR | 0755);
	if (!root)
		return -ENOMEM;

	/* d_make_root consumes the inode reference, including on failure. */
	sb->s_root = d_make_root(root);
	if (!sb->s_root)
		return -ENOMEM;

	return 0;
}

static int vtfs_get_tree(struct fs_context *fc)
{
	struct vtfs_remote_config *config = fc->fs_private;

	if (config->server[0]) {
		if (!config->token[0])
			return -EACCES;
		return get_tree_nodev(fc, vtfs_remote_fill_super);
	}
	return get_tree_nodev(fc, vtfs_fill_super);
}

enum {
	Opt_server, Opt_port, Opt_token, Opt_uid, Opt_gid, Opt_autosync, Opt_cache, Opt_journal,
};

static const struct fs_parameter_spec vtfs_parameters[] = {
	fsparam_string("server", Opt_server),
	fsparam_u32("port", Opt_port),
	fsparam_string("token", Opt_token),
	fsparam_u32("uid", Opt_uid),
	fsparam_u32("gid", Opt_gid),
	fsparam_u32("autosync", Opt_autosync),
	fsparam_u32("cache_mib", Opt_cache),
	fsparam_u32("journal_port", Opt_journal),
	{}
};

static int vtfs_parse_param(struct fs_context *fc, struct fs_parameter *parameter)
{
	struct vtfs_remote_config *config = fc->fs_private;
	struct fs_parse_result result;
	int option = fs_parse(fc, vtfs_parameters, parameter, &result);

	if (option == -ENOPARAM)
		return vfs_parse_fs_param_source(fc, parameter);
	if (option < 0)
		return option;
	switch (option) {
	case Opt_server: {
		u8 address[4];

		if (!in4_pton(parameter->string, -1, address, -1, NULL) ||
		    strscpy(config->server, parameter->string, sizeof(config->server)) < 0)
			return -EINVAL;
		break;
	}
	case Opt_port:
		if (!result.uint_32 || result.uint_32 > 65535)
			return -EINVAL;
		config->port = result.uint_32;
		break;
	case Opt_token: {
		const char *p = parameter->string;

		for (; *p; p++)
			if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
			      (*p >= '0' && *p <= '9') || *p == '-' || *p == '_'))
				return -EINVAL;
		if (strscpy(config->token, parameter->string, sizeof(config->token)) < 0)
			return -EINVAL;
		break;
	}
	case Opt_uid:
		if (!uid_valid(make_kuid(&init_user_ns, result.uint_32)))
			return -EINVAL;
		config->uid = result.uint_32;
		break;
	case Opt_gid:
		if (!gid_valid(make_kgid(&init_user_ns, result.uint_32)))
			return -EINVAL;
		config->gid = result.uint_32;
		break;
	case Opt_cache:
		if (!result.uint_32 || result.uint_32 > 4096)
			return -EINVAL;
		config->cache_mib = result.uint_32;
		break;
	case Opt_journal:
		if (result.uint_32 > 65535)
			return -EINVAL;
		config->journal_port = result.uint_32;
		break;
	case Opt_autosync:
		if (result.uint_32 > 3600)
			return -EINVAL;
		config->autosync = result.uint_32;
		break;
	}
	return 0;
}

static void vtfs_free_context(struct fs_context *fc)
{
	kfree_sensitive(fc->fs_private);
}

static const struct fs_context_operations vtfs_context_ops = {
	.get_tree = vtfs_get_tree,
	.parse_param = vtfs_parse_param,
	.free = vtfs_free_context,
};

static int vtfs_init_fs_context(struct fs_context *fc)
{
	struct vtfs_remote_config *config = kzalloc(sizeof(*config), GFP_KERNEL);

	if (!config)
		return -ENOMEM;
	config->port = 8080;
	config->uid = from_kuid_munged(&init_user_ns, current_fsuid());
	config->gid = from_kgid_munged(&init_user_ns, current_fsgid());
	config->autosync = 1;
	config->cache_mib = 256;
	fc->fs_private = config;
	fc->ops = &vtfs_context_ops;
	return 0;
}

static void vtfs_kill_sb(struct super_block *sb)
{
	if (sb->s_fs_info)
		vtfs_remote_kill_sb(sb);
	else
		kill_anon_super(sb);
}

static struct file_system_type vtfs_fs_type = {
	.owner = THIS_MODULE,
	.name = MODULE_NAME,
	.init_fs_context = vtfs_init_fs_context,
	.parameters = vtfs_parameters,
	.kill_sb = vtfs_kill_sb,
};

static int __init vtfs_init(void)
{
	int ret = register_filesystem(&vtfs_fs_type);

	if (ret)
		return ret;
	LOG("VTFS joined the kernel\n");
	return 0;
}

static void __exit vtfs_exit(void)
{
	int ret = unregister_filesystem(&vtfs_fs_type);

	if (ret)
		pr_err("[vtfs]: unregister_filesystem failed: %d\n", ret);
	else
		LOG("VTFS left the kernel\n");
}

module_init(vtfs_init);
module_exit(vtfs_exit);
