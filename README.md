# Paper Cleaner

`papercleaner` makes a cleaned copy of a LaTeX paper. It removes comments from `.tex` files and deletes assets that no root document references.

## Install

```bash
pip install papercleaner
```

For development:

```powershell
uv sync
```

## Usage

```powershell
papercleaner .\paper
papercleaner .\paper -o .\paper-final
papercleaner .\paper.zip -d
```

```text
usage: papercleaner [-h] [-o <output path>] [-d] [-a] [--keep-eol] [--keep-ws]
                    [-v]
                    <paper directory or archive>
```

### Options

| Option | Description |
| --- | --- |
| `<paper directory or archive>` | Paper directory or archive to process |
| `-o, --output <output path>` | Write to this path instead of the default sibling `-cleaned` path |
| `-d, --deps` | Delete assets not referenced by any root document |
| `-a, --all` | Remove all comments, including magic comments |
| `--keep-eol` | Keep end-of-line comments |
| `--keep-ws` | Keep whitespace exposed by removed end-of-line comments |
| `-v, --version` | Print the version |
| `-h, --help` | Print help |

`-a` and `--keep-eol` cannot be combined. `--keep-ws` works with either option.
