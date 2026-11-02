# Conventions

## Module docstrings

Give new modules a short docstring explaining their purpose and how callers use
them. Keep it accurate when you change the module's responsibility. Include a
short usage example when the entry point is not obvious.

Good:

```python
"""Read strict YAML configuration through load_config(path)."""
```

Bad:

```python
"""Configuration utilities."""
```

## Comments

Use comments to explain why the code needs a choice, constraint or workaround.
Let names and straightforward code explain what it does.

Good:

```python
# YAML 1.1 treats unquoted `on` as a boolean, including mapping keys.
```

Bad:

```python
# Set the key to on.
key = "on"
```

## Error handling

Catch the exceptions you can handle at the boundary that can explain or recover
from them. Preserve the cause when translating an exception into the project's
`AgentError` types. Describe the failed operation and useful next step in an
operator-facing error. A failed read must not silently become an empty result or
successful work.

Good:

```python
try:
    content = path.read_text()
except OSError as exc:
    raise AgentError(f"Cannot read configuration {path}: {exc}") from exc
```

Bad:

```python
try:
    content = path.read_text()
except Exception:
    content = ""
```
