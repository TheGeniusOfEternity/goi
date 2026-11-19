# Contributing

Use English for code comments, documentation, commit messages, and pull requests.

Python components require Python 3.10+ and no third-party dependencies.

```bash
make test
make W=1
```

Python tests use a GitHub fixture and temporary databases. The kernel module was
verified on Linux 7.0.0-43-generic ARM64. Other VFS versions may need adaptation.
Run kernel integration tests in a dedicated VM; see
[the testing instructions](DEVELOPMENT.md#tests).

Add a regression check for behavior changes. Describe the change, verification,
and environment limitations in your PR. Do not commit databases, server-token,
GitHub access tokens, or generated build files.
