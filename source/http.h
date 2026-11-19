/* SPDX-License-Identifier: GPL-2.0 */
#ifndef VTFS_HTTP_H
#define VTFS_HTTP_H
#include <linux/types.h>
#define VTFS_CHUNK_SIZE 32768
#define VTFS_MAX_FILE_SIZE (64ULL * 1024 * 1024)
struct vtfs_remote_config {
	char server[16];
	char token[129];
	u16 port;
	u32 uid;
	u32 gid;
	u32 autosync;
	u32 cache_mib;
	u16 journal_port;
};
int vtfs_http_call(const struct vtfs_remote_config *config, const char *method,
		   const char *query, const void *body, size_t body_len,
		   void *response, size_t capacity, size_t *received);
void encode(const char *src, char *dst);
char *vtfs_url_encode(const char *source);
struct fs_context;
struct super_block;
int vtfs_remote_fill_super(struct super_block *sb, struct fs_context *fc);
void vtfs_remote_kill_sb(struct super_block *sb);
#endif
