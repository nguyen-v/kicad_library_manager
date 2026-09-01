### KiCad Library Manager

An IPC plugin for KiCad for managing local database (sqlite libraries) across many users with a nice UI.

- submit part/category requests via GitHub (`Requests/*.json`)
- fetch/sync a local clone/submodule
- browse/edit parts and browse symbols/footprints with previews
- initialize a brand-new database repo with the required CI scaffolding

**Requirements:** KiCad 9.0+ (PCB Editor), with the **KiCad API** enabled.

This is an **IPC** plugin (`plugin.json`), not a legacy SWIG action plugin. It only appears in the **PCB Editor** toolbar / **Tools → External Plugins**.

---

### 1) Install the plugin

#### 0) One-time prerequisites

**All platforms**

1. Open KiCad → **Preferences → Plugins**
2. Enable **Enable KiCad API** (API server)
3. On Linux, set the Python interpreter to your system `python3` if it is empty (often `/usr/bin/python3`)

**Linux (Debian/Ubuntu) — one-time host packages**

KiCad’s IPC plugin venv needs `pip`/`venv`/`wx`, previews need `rsvg-convert`, and KiCad DBL needs the SQLite ODBC driver. Install everything with:

```bash
sudo ./scripts/setup_linux.sh
```

(PCM install path is usually  
`~/.local/share/kicad/9.0/3rdparty/plugins/com_github_nguyen-v_kicad-library-manager/kicad_library_manager/`.)

On first open, the plugin also detects missing Linux packages and can install them via a password prompt (`pkexec`). You can decline; it won’t ask again until you clear the skip marker or run the script manually.

Equivalent packages if you prefer apt directly:

```bash
sudo apt install python3-pip python3-venv python3-wxgtk4.0 librsvg2-bin unixodbc libsqliteodbc
sudo ./scripts/setup_odbc_linux.sh   # registers "SQLite3 ODBC Driver"
```

#### Recommended: KiCad Plugin and Content Manager (PCM)

1. In KiCad, open **Plugin and Content Manager**
2. Click **Manage...** (bottom left)
3. Add the following repository URL:
   ```
   https://nguyen-v.github.io/kicad_library_manager/repository.json
   ```
4. In the repository dropdown, select **nguyen-v's KiCad PCM repository**
5. Under **Plugins**, find **KiCad Library Manager** and click **Install**
6. Click **Apply Pending Changes**
7. Restart KiCad, then open the **PCB Editor**

On first PCB launch, wait ~30–60s while KiCad creates the plugin environment. The button then appears under **Tools → External Plugins → KiCad Library Manager** (and on the PCB toolbar).

#### Alternative: manual install

Copy or symlink the **entire** repo folder into the KiCad **IPC** plugins directory so that `plugin.json` is at:

```text
…/plugins/kicad_library_manager/plugin.json
```

KiCad IPC plugins directory (KiCad 9):

- **Linux**: `~/.local/share/kicad/9.0/plugins/`
- **macOS**: `~/Documents/KiCad/9.0/plugins/`
- **Windows**: `%USERPROFILE%\Documents\KiCad\9.0\plugins\`

Do **not** install into `scripting/plugins/` — that path is for legacy SWIG plugins and will not register this IPC plugin.

##### Linux / macOS (symlink recommended during development)

```bash
# Replace PLUGDIR with your KiCad IPC plugins directory.
PLUGDIR="$HOME/.local/share/kicad/9.0/plugins"
mkdir -p "$PLUGDIR"
ln -sfn "$(pwd)" "$PLUGDIR/kicad_library_manager"
```

##### Windows (copy recommended)

Copy this `kicad_library_manager/` folder into `%USERPROFILE%\Documents\KiCad\9.0\plugins\`.

Restart KiCad, open the **PCB Editor**, and wait for the first-time plugin environment setup.

#### Troubleshooting: no toolbar icon / empty External Plugins

Usually the plugin was found, but its Python environment failed to finish installing (empty venv / no `pip` / failed deps).

1. Confirm API is enabled (**Preferences → Plugins**).
2. Confirm you are in the **PCB Editor** (not Schematic).
3. Confirm install path contains `plugin.json` under `…/9.0/plugins/…` (not `scripting/plugins`).
4. On Linux, run `sudo ./scripts/setup_linux.sh`, then:

```bash
./scripts/repair_plugin_env.sh
```

5. Fully quit KiCad/PCB and reopen the PCB Editor.

The plugin env lives at:

- Linux: `~/.cache/kicad/9.0/python-environments/com.github.nguyen-v.kicad-library-manager/`
- macOS: `~/Library/Caches/kicad/9.0/python-environments/…`
- Windows: `%LOCALAPPDATA%\kicad\9.0\python-environments\…`

In PCB Editor → **Preferences → Plugins**, you can also right-click the plugin action → **Recreate Plugin Environment**.

---

### 2) Set up / select the database repo

You can use any GitHub repo that contains (or will contain) a KiCad library database layout:

- `Database/` (`db-*.csv`, `parts.sqlite`, `*.kicad_dbl`)
- `Requests/` (request JSON files)
- `Symbols/` and `Footprints/`

Recommended: add your database repo to each project as a submodule under `<project>/Libraries/...`.

---

### 3) Configure the plugin (Settings…)

Open the plugin, then click **Settings…** and set:

- **Local database path**: your local clone/submodule of the database repo
- **Remote database URL**: a git URL or `OWNER/REPO`
- **Branch**: usually `main`
- **DBL filename**: the `Database/*.kicad_dbl` filename to use when initializing a new repo (existing repos can keep any name)

If you created an empty repo for your database, click:

- **Initialize database repo…** (safe-by-default: creates missing files only, never overwrites existing files)  
  It will add workflows + tools + seed files, then commit+push to `origin`.

---

### 3b) Configure KiCad libraries (DBL, symbols, footprints)

This plugin manages a *KiCad database repo*, but KiCad still needs to be told about the libraries it should use.

- **Add the database (DBL) as a Symbol Library**:
  - KiCad → **Preferences → Manage Symbol Libraries…**
  - Add a new library that points to your repo's `Database/*.kicad_dbl` (KiCad "Database Library" / DBL).
- **Add the dependent symbol + footprint libraries**:
  - KiCad → **Preferences → Manage Symbol Libraries…**: add the repo's symbol libraries under `Symbols/` (and any other symbol libraries your database rows reference).
  - KiCad → **Preferences → Manage Footprint Libraries…**: add the repo's footprint libraries under `Footprints/` (and any other footprint libraries your database rows reference).
- **3D models are not generated**:
  - The bundled footprint generator generates `.kicad_mod` footprints only. It does **not** create 3D models.
  - If you want 3D models, add them externally (e.g. step/wrl files + set the 3D model references in KiCad).
- **Footprint generator notes (IPC-7351 / solder goals)**:
  - The footprint generator currently uses IPC-7351-style calculations with solder joint goals defined by PCB Libraries' Footprint Expert tables to meet J-STD-001. See: [Solder Joint Goal Tables (Footprint Expert user guide)](https://www.pcblibraries.com/products/fpx/userguide/default.asp?ch=1.7)
  - It is under active development; **not all package types shown in the dropdown are supported yet**.

---

### 4) GitHub authentication (request submission)

The plugin submits request files using the GitHub API. This is separate from your normal
`git push`/`pull` credentials — the GitHub REST API requires its own token.

The plugin looks for a token in this order:

1. `GITHUB_TOKEN` or `KICAD_LIBRARY_MANAGER_GITHUB_TOKEN` environment variable
2. `gh auth token` (GitHub CLI)
3. GitHub CLI config file (`~/.config/gh/hosts.yml` on Linux/macOS, `%APPDATA%\GitHub CLI\hosts.yml` on Windows)

#### Option A: GitHub CLI (recommended)

Install the GitHub CLI (`gh`), then run `gh auth login` and follow the interactive prompts.

**Windows:**

```
winget install GitHub.cli
```

Or download the installer from [cli.github.com](https://cli.github.com/).

**macOS (Homebrew):**

```bash
brew install gh
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt install gh
```

For other Linux distributions, see the [official install instructions](https://github.com/cli/cli/blob/trunk/docs/install_linux.md).

After installing, open a terminal and run:

```
gh auth login
```

Follow the browser-based login flow. Once complete, the plugin will find your token automatically.

#### Option B: personal access token (environment variable)

If you prefer not to install the GitHub CLI:

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Give it a name (e.g. "KiCad Library Manager") and check the **repo** scope
4. Click **Generate token** and copy it
5. Set it as an environment variable:
   - **Windows**: search "Environment Variables" in the Start menu → User variables → New → Name: `GITHUB_TOKEN`, Value: your token
   - **Linux / macOS**: add `export GITHUB_TOKEN="ghp_your_token_here"` to your `~/.bashrc` or `~/.zshrc`
6. Restart KiCad (it needs to pick up the new environment variable)

---

### 5) ODBC driver setup (required for KiCad DBL, not for the plugin UI)

KiCad's DBL uses an ODBC driver to read `Database/parts.sqlite`. Your DBL connection string expects the driver name:

- `SQLite3 ODBC Driver`

You can verify the registered driver list with:

```bash
odbcinst -q -d
```

#### Windows

Install the 64-bit SQLite ODBC driver:

- Download and run `sqliteodbc_w64.exe` from the upstream SQLite ODBC Driver page:  
  `http://www.ch-werner.de/sqliteodbc/`

After installing, the "SQLite3 ODBC Driver" name is typically available for ODBC connection strings.

#### Linux (Debian/Ubuntu)

Preferred (also installs IPC + preview packages):

```bash
sudo ./scripts/setup_linux.sh
```

ODBC-only:

```bash
sudo ./scripts/setup_odbc_linux.sh
```

This installs `unixodbc` + `libsqliteodbc` and registers the driver in `/etc/odbcinst.ini` under the name
`SQLite3 ODBC Driver`. The plugin will also offer to run this setup on first launch if the driver is missing.

#### macOS (Homebrew)

Run:

```bash
./scripts/setup_odbc_macos.sh
```

This installs the Homebrew `sqliteodbc` formula (which depends on `unixodbc`) and registers the driver name
`SQLite3 ODBC Driver` in the unixODBC configuration. See Homebrew:

- `sqliteodbc`: `https://formulae.brew.sh/formula/sqliteodbc`
- `unixodbc`: `https://formulae.brew.sh/formula/unixodbc`

---

### 5b) Preview rendering dependencies (symbols / footprints)

The plugin’s symbol/footprint previews render SVGs and then rasterize them for display.
On some systems (especially macOS), you may need to install an SVG rasterizer.

#### Linux

On Debian/Ubuntu, `sudo ./scripts/setup_linux.sh` already installs `librsvg2-bin`.
If previews still fail, install just the rasterizer:

```bash
sudo apt install librsvg2-bin
```

- Fedora: `sudo dnf install librsvg2-tools`
- Arch: `sudo pacman -S librsvg`

The plugin prompts to install missing preview tools when it detects the failure.

#### macOS (Homebrew)

```bash
brew install librsvg
```

Alternative:

```bash
brew install --cask inkscape
```

Restart KiCad after installing.

#### Windows

Install Inkscape and ensure `inkscape` is available on `PATH`, then restart KiCad.

---

### Status colors (icons)

- **Green**: up to date / clean
- **Yellow**: local changes or newly submitted requests not yet confirmed
- **Blue**: remote applied your request; sync needed
- **Red**: remote out-of-date vs your local
- **Gray**: unknown/stale (needs Fetch remote)
