// SPDX-License-Identifier: GPL-2.0
/* Lab HTTP client: request, send, receive, parse. */
#include <linux/inet.h>
#include <linux/net.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/vmalloc.h>
#include <net/net_namespace.h>
#include <net/sock.h>
#include "http.h"

#define HEADER_LIMIT 8192

/* The caller frees vec->iov_base after sending the request. */
static int fill_request(struct kvec *vec, const struct vtfs_remote_config *config,
			const char *method, const char *query, bool post, size_t body_size)
{
	char *request_buffer;

	if (strlen(query) > HEADER_LIMIT || body_size > VTFS_MAX_FILE_SIZE)
		return -E2BIG;
	request_buffer = kasprintf(GFP_KERNEL,
		"%s /api/%s?%s HTTP/1.1\r\nHost: %s:%u\r\n"
		"Authorization: Bearer %s\r\nContent-Length: %zu\r\n"
		"Connection: close\r\n\r\n",
		post ? "POST" : "GET", method, query, config->server,
		config->port, config->token, body_size);
	if (!request_buffer)
		return -ENOMEM;
	vec->iov_base = request_buffer;
	vec->iov_len = strlen(request_buffer);
	return 0;
}

/* kernel_sendmsg may send only part of a request. */
static int send_all(struct socket *sock, const void *buffer, size_t size)
{
	while (size) {
		struct msghdr msg = { .msg_flags = MSG_NOSIGNAL };
		struct kvec vec = { .iov_base = (void *)buffer, .iov_len = size };
		int ret = kernel_sendmsg(sock, &msg, &vec, 1, size);

		if (ret <= 0)
			return ret ? ret : -EIO;
		buffer += ret;
		size -= ret;
	}
	return 0;
}

static int receive_all(struct socket *sock, char *buffer, size_t buffer_size)
{
	size_t read = 0;

	while (read < buffer_size) {
		struct msghdr hdr = {};
		struct kvec vec = {
			.iov_base = buffer + read,
			.iov_len = buffer_size - read,
		};
		int ret = kernel_recvmsg(sock, &hdr, &vec, 1, vec.iov_len, 0);

		if (!ret)
			break;
		if (ret < 0)
			return ret;
		read += ret;
	}
	/* The extra byte was allocated by vtfs_http_call, not received from the network. */
	buffer[read] = '\0';
	return read;
}

static int parse_http_response(char *raw_response, size_t raw_response_size,
			       void *response, size_t response_size, size_t *received)
{
	char *end = strnstr(raw_response, "\r\n\r\n", raw_response_size);
	char *buffer = raw_response, *status_line, *header, *body;
	size_t header_size, length = 0;
	bool have_length = false;
	unsigned int status_code;
	__le64 return_value;
	s64 status;

	if (!end)
		return -EPROTO;
	header_size = end + 4 - raw_response;
	if (header_size > HEADER_LIMIT)
		return -E2BIG;
	body = raw_response + header_size;
	*end = '\0';

	/* Read the response line, then the headers, as in the lab template. */
	status_line = strsep(&buffer, "\n");
	if (sscanf(status_line, "HTTP/%*u.%*u %u", &status_code) != 1 || status_code != 200)
		return -EPROTO;
	while ((header = strsep(&buffer, "\n"))) {
		if (!strncasecmp(header, "Content-Length:", 15)) {
			unsigned long value;

			if (have_length || kstrtoul(strim(header + 15), 10, &value))
				return -EPROTO;
			length = value;
			have_length = true;
		} else if (!strncasecmp(header, "Transfer-Encoding:", 18)) {
			return -EPROTO;
		}
	}
	if (!have_length || length < sizeof(return_value) ||
	    length > raw_response_size - header_size)
		return -EPROTO;

	memcpy(&return_value, body, sizeof(return_value));
	status = (s64)le64_to_cpu(return_value);
	if (status)
		return status < 0 && status >= -4095 ? (int)status : -EPROTO;
	length -= sizeof(return_value);
	if (length > response_size)
		return -ENOSPC;
	if (length)
		memcpy(response, body + sizeof(return_value), length);
	*received = length;
	return 0;
}

int vtfs_http_call(const struct vtfs_remote_config *config, const char *method,
		   const char *query, const void *body, size_t body_len,
		   void *response_buffer, size_t buffer_size, size_t *received)
{
	struct socket *sock;
	struct sockaddr_in s_addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = in_aton(config->server),
		.sin_port = htons(config->port),
	};
	struct kvec vec;
	size_t raw_buffer_size = HEADER_LIMIT + max_t(size_t, buffer_size, 4096) + sizeof(__le64);
	char *raw_response_buffer;
	int error, read_bytes;

	*received = 0;
	error = fill_request(&vec, config, method, query, body != NULL, body_len);
	if (error)
		return error;
	raw_response_buffer = kvmalloc(raw_buffer_size + 1, GFP_KERNEL);
	if (!raw_response_buffer) {
		error = -ENOMEM;
		goto free_request;
	}
	error = sock_create_kern(&init_net, AF_INET, SOCK_STREAM, IPPROTO_TCP, &sock);
	if (error)
		goto free_response;
	sock->sk->sk_rcvtimeo = msecs_to_jiffies(10000);
	sock->sk->sk_sndtimeo = msecs_to_jiffies(10000);
	error = kernel_connect(sock, (struct sockaddr_unsized *)&s_addr, sizeof(s_addr), 0);
	if (error)
		goto release;

	error = send_all(sock, vec.iov_base, vec.iov_len);
	if (!error && body_len)
		error = send_all(sock, body, body_len);
	if (error)
		goto release;
	read_bytes = receive_all(sock, raw_response_buffer, raw_buffer_size);
	if (read_bytes < 0) {
		error = read_bytes;
		goto release;
	}
	error = parse_http_response(raw_response_buffer, read_bytes, response_buffer,
				    buffer_size, received);
release:
	sock_release(sock);
free_response:
	kvfree(raw_response_buffer);
free_request:
	kfree_sensitive(vec.iov_base);
	return error;
}

/* Percent encoding from the lab template. dst needs 3 * strlen(src) + 1 bytes. */
void encode(const char *src, char *dst)
{
	while (*src != '\0') {
		if ((*src >= '0' && *src <= '9') || (*src >= 'a' && *src <= 'z') ||
		    (*src >= 'A' && *src <= 'Z')) {
			*dst++ = *src;
		} else {
			sprintf(dst, "%%%02X", (unsigned char)*src);
			dst += 3;
		}
		src++;
	}
	*dst = '\0';
}

char *vtfs_url_encode(const char *source)
{
	size_t length = strlen(source);
	char *result;

	if (length > 4096)
		return ERR_PTR(-ENAMETOOLONG);
	result = kmalloc(length * 3 + 1, GFP_KERNEL);
	if (!result)
		return ERR_PTR(-ENOMEM);
	encode(source, result);
	return result;
}
