# EndeavourOS Workstation Setup

Turn a fresh **EndeavourOS (x86_64)** install into a ready-to-use development workstation with a handful of commands, and keep it in that state afterwards.

Three tools split the work:

- **Ansible** (this repo) sets up the system: official Arch/EndeavourOS packages via `pacman`, AUR packages via `yay`, compilers and cross-compilers, Podman, browsers, desktop and vendor apps, AI coding CLIs, and Snapper snapshots.
- **Chezmoi** restores your personal dotfiles from a private Git repo, with secrets encrypted by `age`.
- **Mise** installs the language runtimes and developer CLIs pinned in those dotfiles (Go, Node, Rust, Neovim, ripgrep, and so on).

Everything is safe to re-run. A second run changes nothing unless something has drifted.

## Before you start

- **EndeavourOS (or Arch Linux) on x86_64.**
- **A normal desktop user with `sudo` rights (`wheel` group).** Run every command below as that user, *not* with `sudo` (except the standalone `preboot/` scripts). You'll be asked for your password when it's needed.
- **EndeavourOS defaults assumed for Snapper/hibernation:** Btrfs root (`@` and `@home` subvolumes created by Calamares) with `systemd-boot` (`/efi`) and `dracut` (`/etc/kernel/cmdline`, `reinstall-kernels`). On non-Btrfs filesystems, turn Snapper off in step 3.
- **`yay`**, preinstalled on EndeavourOS for AUR packages (`enable_aur: false` skips AUR packages if needed).
- *Optional:* a **private Chezmoi dotfiles repo** plus a backup of its **age key** (`key.txt`), if you want your personal setup restored.

## Set up a new machine

### 1. Install EndeavourOS with Btrfs and systemd-boot

In the EndeavourOS installer (Calamares):

- Choose **systemd-boot** (the default on modern EndeavourOS ISOs, which puts the EFI System Partition at `/efi` and builds initramfs images with `dracut`).
- Choose **Btrfs** for the root filesystem (Calamares creates `@` and `@home` subvolumes automatically; no live-USB subvolume migration is needed).
- On laptops where you plan to enable hibernation (`enable_hibernation: true`), select **no swap** or a regular swap partition during install—the `snapper` role creates and activates an isolated `@swap/hibernate.swap` subvolume file later.

### 2. Update, clone, and bootstrap

After booting into the installed system, bring it fully up to date first so the first `apply` doesn't replace the running kernel under you:

```bash
yay -Syu && sudo reboot
```

Then clone the repo into `~/github`. The `update` shell function in the dotfiles and the STM32 download folder both expect that location.

```bash
mkdir -p ~/github && cd ~/github
git clone https://github.com/dsscheib/endeavoros-system-build.git
cd endeavoros-system-build
./bootstrap.sh
```

This installs pacman prerequisites, then `uv`, `mise`, and pinned versions of `ansible-core` and `ansible-lint` into `~/.local/bin` (downloads are checksum-verified). If your shell then says `mise: command not found`, run `export PATH="$HOME/.local/bin:$PATH"` or log out and back in.

### 3. Choose your settings

```bash
cp ansible/group_vars/all/local.yml.example ansible/group_vars/all/local.yml
```

Edit `local.yml` (it's gitignored, so it stays on this machine). The settings you're most likely to change:

| Setting | Default | Change it when… |
| :--- | :--- | :--- |
| `dotfiles_repo` | empty | you want `mise run auth` to restore your private Chezmoi dotfiles. Use the HTTPS URL: SSH keys don't exist yet on a fresh machine. |
| `nvim_config_repo`, `nvim_config_ref` | empty | you want your Neovim config cloned into `~/.config/nvim` (`ref` is an optional branch, tag or commit). Use the HTTPS URL. If the repo is private, leave this empty until after step 5, because there are no GitHub credentials before then. |
| `enable_snapper` | `true` | set `false` if `/` is not Btrfs `subvol=@`; otherwise `apply` stops with a Btrfs layout error |
| `enable_hibernation` | `false` | set `true` on laptops to create a `@swap/hibernate.swap` Btrfs swapfile and dracut/systemd-boot resume settings. Closing the lid on battery then suspends, and hibernates after 1 hour (`HIBERNATE_DELAY` in `scripts/configure_hibernation.py`); on AC it only suspends. |
| `enable_aur` | `true` | set `false` to skip AUR packages installed via `yay` (`google-chrome`, `visual-studio-code-bin`, `claude-code`, `synology-drive`, `dropbox`, `avra`, `litecli`, `mycli`, `appimagelauncher`) |
| `install_samsung_970_evo_nvme_rule` | `false` | only on the machine with that Samsung drive at PCI `0000:00:1d.0` |
| `install_dropbox` | `true` | set `false` to skip the `dropbox` AUR package |
| `install_appimagelauncher` | `false` | set `true` to install `appimagelauncher` from AUR |
| `install_kicad_3d_libraries` | `true` | set `false` to skip the large `kicad-library-3d` package |
| `install_cosmic_swipe` | `true` | set `false` to skip [cosmic-swipe](https://github.com/dsscheib/cosmic-swipe) on COSMIC desktops. It's skipped automatically on KDE Plasma or other desktops without `/usr/bin/cosmic-comp`. |

Everything else (package lists, Flatpak apps, which STM32 tools to install, and so on) lives in `ansible/group_vars/all/00-defaults.yml`. Any of it can be overridden in `local.yml`. Note that package/udev toggles (`install_dropbox`, `install_appimagelauncher`, `install_kicad_3d_libraries`, `install_samsung_970_evo_nvme_rule`) are add-only once enabled, whereas setting `install_cosmic_swipe: false` removes its managed files, service, and udev rule.

**Restoring encrypted dotfiles?** Put your age key in place now, before step 5:

```bash
install -d -m 0700 ~/.config/chezmoi
install -m 0600 /path/to/backup/key.txt ~/.config/chezmoi/key.txt
```

### 4. First apply

```bash
mise run plan    # optional: preview what will change, without changing anything
mise run apply
```

When Ansible asks for `BECOME password:`, enter your sudo password. The first run downloads and installs a lot, so expect it to take a while.

### 5. Sign in

**Restoring a backup of your home directory?** Do it before this step, and leave `~/.local/share/keyrings/` out of it. An old keyring replaces the new one and wipes the GitHub CLI login this step creates.

```bash
mise run auth
```

This walks through each interactive sign-in and skips any step that doesn't apply:

1. **GitHub CLI**: signs in through your browser.
2. **Dotfiles**: runs Chezmoi with `dotfiles_repo`, which also installs your Mise runtimes and CLIs.
3. **Google Drive**: if your dotfiles provide the OAuth client, signs in to rclone and asks before the first full two-way sync.
4. **Herdr**: hooks Herdr into whichever agent CLIs are installed.

To redo a single step later: `mise run auth -- --step gdrive` (or `github`, `chezmoi`, `herdr`). Every step is safe to rerun if one stops partway, for example on a network error or a closed browser tab.

### 6. Apply again

```bash
mise run apply
```

Now that your dotfiles and Mise tools are in place, this second run sets up everything that depends on them: Neovim and tmux plugins, shell completions, and Herdr skill files.

### 7. Finish up

- **Log out and back in** so your new `uucp` and `plugdev` group memberships apply (needed for serial ports and debug probes on Arch).
- **Laptops with hibernation:** reboot, then test with `systemctl hibernate`, and once more with `systemctl suspend-then-hibernate`.
- **Sign in to the AI CLIs** you use: `agy`, `claude`, `codex`, `copilot`, and `:Copilot auth` inside Neovim.
- **Dropbox:** launch `dropbox` once and sign in to this computer.

## Everyday use

| I want to… | Run |
| :--- | :--- |
| See what has drifted from the configuration | `mise run plan` |
| Bring the system back in line, or pick up changes to this repo | `mise run apply` |
| Run just one area | `mise run role browsers` (several at once: `mise run role gcc,llvm`) |
| Update installed software (Pacman/AUR, Flatpak, Neovim and tmux plugins; reports firmware and Mise updates) | `mise run update` |
| Pull dotfile changes (also installs updated Mise tools) | `chezmoi update` |
| Upgrade runtimes and CLIs pinned in your dotfiles | `mise run update -- --only mise --bump-mise`, then review and commit your dotfiles |
| Check this repo's pinned downloads for new releases | `mise run pins` (update one with `mise run pins -- --bump NAME`) |

Extra Ansible options go after `--`, for example `mise run apply -- --diff` or `mise run plan -- --tags system`. See [docs/updating.md](docs/updating.md) for what each update covers and how to roll back.

### Roles and tags

Use any of these names with `mise run role <name>`:

| Role (extra tags) | What it sets up |
| :--- | :--- |
| `system` (`systemd_live`) | optional Pacman system upgrades (`system_pacman_upgrade: true`), UFW firewall (`ufw.service`), `uucp`/`plugdev` groups, `systemd-oomd`, journald retention, GitHub CLI, optional NVMe udev rule |
| `snapper` (`hibernation`, `systemd_live`) | `@snapshots` subvolume at `/.snapshots`, Snapper root snapshots (`wheel` group), `snap-pac` pacman hooks, `btrfs-assistant`, `snapper-rollback`, optional dracut/systemd-boot hibernation |
| `toolchains` (`gcc`, `llvm`, `arm`, `avr`, `riscv`, `arm-avr`) | `base-devel` + GCC/G++, Clang/LLVM/LLDB/LLD, plus ARM, AVR and RISC-V cross-compilers |
| `containers` (`podman`, `systemd_live`) | Rootless Podman with `docker.io` as the default search registry, plus Fish completion |
| `browsers` (`firefox`, `chrome`) | Firefox Developer Edition (`firefox-developer-edition`) and Google Chrome (`google-chrome` via AUR) |
| `desktop_apps` (`desktop_gui`, `fonts`, `vscode`, `kicad`, `desktop_packages`, `flatpak`, `arduino`, `saleae`, `synology`, `hyper_backup`, `appimagelauncher`, `cosmic_swipe`, `systemd_live`) | `ttf-jetbrains-mono-nerd`, VS Code (AUR), KiCad + `kicad-python`, FreeCAD and OrcaSlicer (Flathub), Arduino IDE 2, Saleae Logic 2, Synology Drive (AUR), Hyper Backup Explorer, Dropbox (AUR), optional AppImageLauncher (AUR), and on COSMIC the cosmic-swipe four-finger gestures |
| `ai_agents` (`claudecode`, `antigravity`, `herdr`) | Claude Code (`claude-code` via AUR) and the Antigravity CLI (`agy`), with Antigravity set to ask before destructive git commands |
| `workstation` (`chezmoi`, `mise`, `neovim`, `tmux`, `herdr`) | Applies dotfile changes, installs missing Mise tools, sets up the Neovim Python provider and config, and installs tmux plugins |
| `completions` | Shell completions for `codex`, `herdr`, `rustup` and `cargo` |
| `stm32` | ST-LINK udev rules and STMicroelectronics tools (opt-in; see below) |

## Optional extras

### STMicroelectronics tools (STM32)

ST requires a sign-in to download its tools, so they're never part of a normal `apply`.

1. Download the installers from st.com. Only the exact versions listed in `resources/st-installers.sha256` are accepted:
   - CubeIDE 2.2.0 (extracted cleanly in Python without `dpkg-deb`)
   - CubeMX 6.18.1
   - CubeProgrammer
   - System Workbench (SW4STM32)
2. Put them in `~/Downloads`, in `$ST_DOWNLOAD_DIR` (default `~/github/st-microelectronics`), or in the repo folder.
3. Run `mise run role stm32`.

Tools are installed under `~/STMicroelectronics` with launchers in your app menu, and previous versions are kept. If an installer isn't found, that tool is skipped with a warning. To install only some of the tools, set `stm32_tools` in `local.yml` to a subset of `cubeide`, `cubemx`, `cubeprog` and `sw4stm32`.

### Secondary Btrfs data drive

```bash
sudo ./preboot/setup_secondary_btrfs_drive.sh                 # reattach an existing data pool
sudo ./preboot/setup_secondary_btrfs_drive.sh --format        # ERASES the drive and creates a new pool
sudo ./preboot/setup_secondary_btrfs_drive.sh /dev/disk/by-id/<your-drive>
```

This mounts the drive's `@data` and `@snapshots` subvolumes at `/mnt/data` and links `~/data` and `~/Downloads` to it. It refuses to touch the system disk (including `/efi`).

The script was written for one machine's Samsung 970 EVO. With no drive given, it looks for that drive; for any other drive, pass its `/dev/disk/by-id/…` path. It always installs the Samsung power-management udev rule.

### Snapper without Ansible

```bash
sudo ./preboot/setup_btrfs_snapper.sh                     # Snapper plus hibernation
sudo ./preboot/setup_btrfs_snapper.sh --skip-hibernation  # Snapper only
```

This does the same job as the `snapper` role, for a machine where you don't want to run the full playbook.

## Troubleshooting

| Problem | Fix |
| :--- | :--- |
| `mise: command not found` right after bootstrap | `export PATH="$HOME/.local/bin:$PATH"`, or log out and back in |
| `apply` stops with "Root must mount Btrfs subvolume /@" | Your root isn't the Btrfs `@` layout from step 1. Set `enable_snapper: false` in `local.yml`. |
| The containers role asks for subordinate IDs | Run the `sudo usermod --add-subuids … --add-subgids …` command it prints, then `mise run role containers` |
| Chezmoi can't decrypt files during `mise run auth` | Your age key isn't at `~/.config/chezmoi/key.txt` (see step 3) |
| `mise run auth` says `GDRIVE_CLIENT_ID` isn't a Google OAuth client ID, or the browser says "The OAuth client was not found" | `~/.config/rclone/gdrive-oauth.env` in your dotfiles still holds placeholder values, or the client was deleted. Put your Google Cloud OAuth client ID and secret in it, run `chezmoi add --encrypt ~/.config/rclone/gdrive-oauth.env`, then `mise run auth -- --step gdrive` |
| "Permission denied" on a serial port or debug probe | Log out and back in to pick up the `uucp` and `plugdev` groups, then reconnect the device |
| GitHub CLI suddenly signed out after restoring a backup | Don't restore an old `~/.local/share/keyrings/` over a new install, because it replaces the GitHub CLI token. Sign in again with `mise run auth -- --step github`. |

## How the repo is organized

```text
bootstrap.sh            Installs uv, mise, ansible-core and Galaxy collections
mise.toml               The `mise run …` tasks
ansible/                Playbook, roles, tasks, and settings (group_vars/all/)
preboot/                Standalone Snapper and secondary Btrfs drive scripts (run as root)
install_stm32_*.sh      STMicroelectronics installers used by the stm32 role
scripts/                Helpers used by the roles, plus auth.sh, check.sh and container tests
resources/              Static files: udev rules, journald config, checksums, icons
tests/                  Unit tests for the scripts
docs/                   Guides and status notes: updating.md, phase4-chezmoi-guide.md
```

Runtimes and developer CLIs (`go`, `node`, `rust`, `uv`, `neovim`, `starship`, `ripgrep`, `fd`, `bat`, `fzf`, `zoxide`, `herdr`, `codex`, `copilot` and more) are deliberately **not** managed here. They live in your private dotfiles repo as `~/.config/mise/config.toml` and `mise.lock`. See [docs/phase4-chezmoi-guide.md](docs/phase4-chezmoi-guide.md) for how that repo is laid out.

## Checking changes to this repo

```bash
mise run check    # or: bash scripts/check.sh
```

This runs `shellcheck`, Bash syntax checks, Python and YAML parsing, `ansible-playbook --syntax-check`, `ansible-lint`, and the unit tests. CI runs the same checks, plus a full playbook test in `archlinux:latest`. That test previews the changes, applies them, then confirms that both a second preview and a second apply report no changes.

To run the full greenfield end-to-end test locally in a disposable `archlinux:latest` container (requires Podman and network access; exercises `bootstrap.sh`, `desktop_apps` minus AUR/Flatpak, `auth.sh` with ephemeral fixtures, Day-2 convergence, `lazy-lock.json` merge, `stm32`, and `mise run role`):

```bash
podman run --rm -v "$PWD:/workspace:ro,Z" docker.io/library/archlinux:latest \
  bash /workspace/scripts/e2e_container_test.sh /workspace
```

For the faster CI-only container test (skips `desktop_gui`, `auth.sh`, and `stm32`), substitute `scripts/ci_container_test.sh`.
