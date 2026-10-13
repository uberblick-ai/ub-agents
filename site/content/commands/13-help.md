# ub-agents help

Run `ub-agents help` for the command overview, or `ub-agents help COMMAND` for details. It works without configuration or network access.

The bare command, `help`, `-h` and `--help` show the overview and exit 0.
`help COMMAND`, `COMMAND -h` and `COMMAND --help` show the same command help.
`-v` and `--version` print the installed version. Help creates no files.

Usage errors exit 2 on stderr: unknown commands include the overview; other errors
include the command's usage line. `retry` requires a positive number;
the deprecated hidden `--number` alias cannot be combined with a positional number.

```text
{{help help}}
```
