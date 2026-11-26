# Keeping the workstation up to date

Updates come in two kinds:

- **Floating software** (Arch/EndeavourOS repositories, AUR packages, Flatpaks, Neovim and tmux plugins) always takes the newest release. `mise run update` handles it.
- **Pinned versions** stay exactly where they are until you bump them on purpose. That covers your dev tools in the dotfiles' Mise config, and the vendor downloads and commit pins in this repo. The pins are what let a rebuild reproduce this machine cleanly, and they are the only integrity check for downloads that don't come from Pacman.

A good rhythm: run `mise run update` weekly, and bump pins about once a month.

## Everyday: `mise run update`

| Step | What it does |
| :--- | :--- |
| `pacman` | `yay -Syu` (or `sudo pacman -Syu` if `yay` is absent). Shows the plan and asks first; `--yes` skips the question (`--noconfirm`). Warns if the running kernel modules directory changed so you know to reboot. |
| `flatpak` | `flatpak update` for user and system installs |
| `firmware` | Lists available firmware updates. It never installs them; run `fwupdmgr update` yourself when you're ready to reboot. |
| `nvim` | `Lazy! sync`: updates Neovim plugins and `~/.config/nvim/lazy-lock.json` |
| `tmux` | Updates tmux plugins through TPM |
| `mise` | Lists dev tools with newer releases than your pinned versions (report only) |

Options: `--yes`, `--skip STEP[,STEP]`, `--only STEP[,STEP]`, `--bump-mise`. For example, `mise run update -- --skip firmware,nvim`. Run `mise run update -- --help` for the full list (without the `--`, Mise shows its own task help instead).

Pacman upgrades are covered by Snapper: `snap-pac` takes a snapshot before and after every transaction (`snapper list` shows them, `btrfs-assistant` browses them, and `snapper-rollback` restores one).

**Rolling back across a kernel update:** on EndeavourOS with `systemd-boot` and `dracut`, kernels and initramfs images live on the FAT32 EFI partition (`/efi`), which Btrfs snapshots don't cover. After a rollback to a snapshot with an older kernel, the `/efi` kernel wouldn't match the restored root's `/usr/lib/modules`. Boot from an EndeavourOS live USB, mount `@` at `/mnt` and the EFI partition at `/mnt/efi`, chroot with `sudo arch-chroot /mnt`, and run `reinstall-kernels` so `/efi` matches the restored root snapshot.

To make your existing `update` alias use this, point it at this repo in your dotfiles' fish config:

```fish
alias update="mise -C ~/github/endeavoros-system-build run update"
```

## Bumping your dev tools (Mise, in your dotfiles)

```bash
mise run update -- --only mise                # see what's behind
mise run update -- --only mise --bump-mise    # bump everything, then re-add the config to Chezmoi
chezmoi git -- status                      # review (new locks/ folders show as untracked)
chezmoi git -- add -A && chezmoi git -- commit -m "Bump Mise tools" && chezmoi git -- push
```

`--bump-mise` moves every tool to its newest release, **including new major versions**. To bump only some tools, run it by hand from your home directory: `mise upgrade --bump uv rust`, then `chezmoi add ~/.config/mise`. Use `add`, not `re-add`: npm and pipx tools get a new `locks/<tool>/<version>/` folder that `re-add` skips, and without it `mise install` fails on your other machines. If a bump breaks something, revert the dotfiles commit and run `chezmoi apply`; Mise reinstalls the previous versions.

## Bumping this repo's pins

```bash
mise run pins                       # report every pin: current, latest, and what to do
mise run pins -- --bump saleae uv   # update named pins in place
mise run pins -- --bump-all         # update every pin that can be bumped automatically
```

For each download, a bump fetches the new release, computes its SHA-256 (and cross-checks published upstream digests when available, such as GitHub SHA-256 or Antigravity SHA-512), and edits `downloads.yml`, `00-defaults.yml`, `bootstrap.sh` or `requirements.yml`.

When `mise run apply` later installs the new version of Arduino IDE, Saleae Logic, Hyper Backup Explorer, `cosmic-swipe`, or the Antigravity CLI, it removes the previous versions once the new one is linked (the `prune_versions` role). It never removes anything if the new version isn't there.

STM32 installers in `resources/st-installers.sha256` remain manual because ST requires a sign-in to download them.

After bumping, do it like any other change:

1. Work on a branch.
2. Review `git diff`.
3. Run `mise run check`.
4. CI's container test skips desktop GUI apps, so for pins in `downloads.yml` also run the full end-to-end test in `archlinux:latest` (needs a normal clone, because it runs `git diff --check`):
   `podman run --rm -v "$PWD:/workspace:ro,Z" docker.io/library/archlinux:latest bash /workspace/scripts/e2e_container_test.sh /workspace`
5. Open a PR so CI runs the container test.
6. Check with `mise run plan`, then `mise run apply`.

## Antigravity CLI (`agy`)

`agy` updates itself automatically in the background during normal runs (opt out with `export AGY_CLI_DISABLE_AUTO_UPDATE=true`). You can also trigger an update immediately anytime with `agy update` (and view release notes with `agy changelog` or `/changelog`). `mise run update` does not upgrade `agy`.

Self-updates replace the binary in place inside `~/.local/share/eos-workstation/.antigravity-cli.versions/<pinned-version>/`. Future `mise run apply` runs only check that the binary exists, so they won't revert an in-place self-update while the `antigravity_cli` pin in `ansible/group_vars/all/downloads.yml` stays the same.

To update the baseline version installed on fresh machines, run `mise run pins -- --bump antigravity_cli` (which reads Google's Linux x64 release manifest and cross-checks its SHA-512 digest), then apply normally—changing the pin switches the `antigravity-cli` symlink to the newly pinned version directory.
