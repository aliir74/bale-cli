# bale-cli

[![GitHub Sponsors](https://img.shields.io/badge/sponsor-30363D?style=for-the-badge&logo=GitHub-Sponsors&logoColor=#EA4AAA)](https://github.com/sponsors/aliir74)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://www.buymeacoffee.com/aliir74)

A small command-line client for [Bale](https://bale.ai) (بله) that acts as your own **user account**, not a bot. Built on [aiobale](https://github.com/aminmadaniofficial/aiobale), pinned to a reviewed commit and installed from GitHub (the PyPI name `aiobale` is an unrelated placeholder).

> Bale has no official user-account API. This uses its private web API, so heavy or bulk use may get the account restricted. Use it for personal, low-volume messaging.

## Install

```sh
uv tool install --python 3.12 git+https://github.com/aliir74/bale-cli
```

## Log in

```sh
bale auth send-code --phone 0912xxxxxxx
bale auth verify --code 12345        # two-step password, if any: export BALE_2FA_PASSWORD=...
bale auth status
```

The session is stored in `~/.bale/session.bale` (mode 600). It grants full access to the account; keep it private. Override the folder with `BALE_HOME`.

## Use

Targets: `me` (Saved Messages), `g:<id>` (group), `u:<id>` (user), `c:<id>` (channel), or a bare id seen in the last `chats list`.

```sh
bale chats list --limit 60            # recent dialogs, with titles and member counts
bale chats list --groups
bale chats search "family"
bale messages list --chat g:123456 --limit 20
bale send text  --to me "hello"
bale send gif   --to g:123456 --caption "..." ./funny.gif
bale send photo --to g:123456 ./pic.jpg
bale send file  --to u:987654 ./doc.pdf
```

Add `--json` for structured output.

## Notes

- aiobale's models reject some live dialogs and deleted-account users; the CLI decodes `LoadDialogs` and `LoadUsers` raw to stay working.
- `send gif` reads size, duration and a small thumbnail with Pillow so the GIF plays inline.

## Support This Project

If you find this project useful, consider supporting its development:

- Star this repo
- Report bugs and submit PRs
- [Sponsor on GitHub](https://github.com/sponsors/aliir74) or [Buy me a coffee](https://www.buymeacoffee.com/aliir74)
