# Remote PowerShell: quoting and launching

Read this before you change `scripts/cross_pc_ssh.py` or write any other PowerShell that runs on a peer.
The helper already follows these rules; keep them when you edit it.

## Strings: embed them as base64

- **A single-quoted literal is also closed by U+2018–U+201B**, not only by `'`. Doubling `'` doesn't stop
  a path such as `a’+(Write-Host X)+’` from running code. Embed every string (paths, git arguments, roots,
  the prompt, cached paths) as `([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('<b64>')))`.
  Base64 has no quote characters.
- **Send the script itself on stdin**, base64-encoded, behind a fixed `-EncodedCommand` bootstrap that
  reads stdin and runs it. Only the bootstrap goes on the command line, so cmd.exe's 8191-character limit
  never applies, and stdin closes after the write, so a nested ssh can't hang on it. `exit N` inside the
  script still sets ssh's exit code.

Why: both bugs were real. The first version of this helper, and a competing one, let a path or a git
argument run code on the peer.

## Arguments to native programs (git, claude, codex)

- **Windows PowerShell 5.1 drops embedded double quotes** when it builds a native command line, and a
  trailing `\` joins the next argument. So `-"-output=x"` passes a check for options starting `--` and
  reaches git as `--output=x`. Refuse any argument that contains `"` or ends in `\`, before quoting it.
- Check options after that normalization. Refuse prefixes of denied long options, because git accepts
  abbreviations (`--outp=`, `--open=`).

## Launching a CLI on the peer

- Resolve it with `Get-Command <cli> -CommandType Application` (.exe/.cmd). Under `-NoProfile` a bare
  `codex` resolves to npm's `codex.ps1`, which the default execution policy blocks. PowerShell then
  prints an error but **still exits 0**, so the failure looks like an empty answer.

## Verify

- Run the generated scripts through the same bootstrap in local Windows PowerShell (the skill's tests do
  this): string round-trips, Unicode-quote paths, and the exit code.
- For an agent run, ask for a file write in read-only mode and check that no file appeared.
