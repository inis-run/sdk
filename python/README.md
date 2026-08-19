# inis.run Python SDK

Run untrusted or AI-generated code in its own isolated VM.

[![PyPI](https://img.shields.io/pypi/v/inis.svg)](https://pypi.org/project/inis/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/inis-run/sdk/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/inis/)

## Install

```bash
pip install inis
```

## Quickstart

```python
from inis import Client

client = Client()  # reads INIS_API_KEY (and INIS_BASE_URL) from the environment

with client.session() as session:
    session.exec(["pip", "install", "httpx", "-q"])
    session.files.write("/workspace/out.txt", "hello")
    print(session.files.read("/workspace/out.txt"))

    preview = session.expose(8765)
    print(preview.preview_url)

# Stateless execution (throwaway session, no cleanup needed)
result = client.execute(language="python", code="print(42)")
print(result.stdout)

# Re-attach to a session created elsewhere, by ID
session = client.sessions.attach("9hmquuxiz7qyqdqiwfvep")
```

`client.session()` returns a context manager that destroys the session on
exit. Use `client.sessions.create(...)` directly if you want to manage its
lifetime yourself, and `client.sessions.attach(id)` to reconnect to a
session you don't own — that call never destroys it.

## Timeouts

`Client(timeout=...)` and `Session(timeout=...)` are **seconds** (httpx
convention). The TypeScript SDK's `timeoutMs` is milliseconds (JS
convention) — a deliberate per-language idiom, not drift.

## Links

- Docs: [docs.inis.run](https://docs.inis.run)
- Quickstart: [docs.inis.run/docs/quickstart](https://docs.inis.run/docs/quickstart)
- Source: [github.com/inis-run/sdk/tree/main/python](https://github.com/inis-run/sdk/tree/main/python)
- Homepage: [inis.run](https://inis.run)

Built and run in the EU. Your code and data never leave Europe.

## License

MIT — see [LICENSE](https://github.com/inis-run/sdk/blob/main/LICENSE).
