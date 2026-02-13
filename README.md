### KiCad Library Manager

The plugin is a plugin for managing a KiCad parts library repository

- submit part/category requests via GitHub (`Requests/*.json`)
- fetch/sync a local clone/submodule
- browse/edit parts and browse symbols/footprints with previews
- initialize a brand-new database repo with the required CI scaffolding

---

### 1) Install the plugin bundle

Install by copying (or symlinking) the **entire** `kicad_library_manager/` folder into your KiCad IPC plugins directory
(the folder that will contain `plugin.json`).

KiCad’s IPC plugins directory is typically under your KiCad documents home, e.g.:

- **Linux**: `~/.local/share/kicad/9.0/plugins/`
- **macOS**: `~/Documents/KiCad/9.0/plugins/`
- **Windows**: `%USERPROFILE%\\Documents\\KiCad\\9.0\\plugins\\`

#### Linux / macOS (symlink recommended during development)

```bash
# Replace PLUGDIR with your KiCad IPC plugins directory.
PLUGDIR="$HOME/.local/share/kicad/9.0/plugins"
mkdir -p "$PLUGDIR"
ln -sfn "$(pwd)" "$PLUGDIR/kicad_library_manager"
```

#### Windows (copy recommended)

Copy this `kicad_library_manager/` folder into your KiCad IPC plugins folder.

Restart KiCad. The plugin is available under **Tools → External Plugins → KiCad Library Manager**
(menu location may vary slightly between editors).

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
  - Add a new library that points to your repo’s `Database/*.kicad_dbl` (KiCad “Database Library” / DBL).
- **Add the dependent symbol + footprint libraries**:
  - KiCad → **Preferences → Manage Symbol Libraries…**: add the repo’s symbol libraries under `Symbols/` (and any other symbol libraries your database rows reference).
  - KiCad → **Preferences → Manage Footprint Libraries…**: add the repo’s footprint libraries under `Footprints/` (and any other footprint libraries your database rows reference).
- **3D models are not generated**:
  - The bundled footprint generator generates `.kicad_mod` footprints only. It does **not** create 3D models.
  - If you want 3D models, add them externally (e.g. step/wrl files + set the 3D model references in KiCad).
- **Footprint generator notes (IPC-7351 / solder goals)**:
  - The footprint generator currently uses IPC-7351-style calculations with solder joint goals defined by PCB Libraries’ Footprint Expert tables to meet J-STD-001. See: [Solder Joint Goal Tables (Footprint Expert user guide)](https://www.pcblibraries.com/products/fpx/userguide/default.asp?ch=1.7)
  - It is under active development; **not all package types shown in the dropdown are supported yet**.

---

### 4) GitHub authentication (request submission)

The plugin submits request files using the GitHub API. It looks for a token in this order:

- `GITHUB_TOKEN` or `KICAD_LIBRARY_MANAGER_GITHUB_TOKEN` environment variable
- `gh auth token` (GitHub CLI)
- `~/.config/gh/hosts.yml` token (GitHub CLI config)

---

### 5) ODBC driver setup (required for KiCad DBL, not for the plugin UI)

KiCad’s DBL uses an ODBC driver to read `Database/parts.sqlite`. Your DBL connection string expects the driver name:

- `SQLite3 ODBC Driver`

You can verify the registered driver list with:

```bash
odbcinst -q -d
```

#### Windows

Install the 64-bit SQLite ODBC driver:

- Download and run `sqliteodbc_w64.exe` from the upstream SQLite ODBC Driver page:  
  `http://www.ch-werner.de/sqliteodbc/`

After installing, the “SQLite3 ODBC Driver” name is typically available for ODBC connection strings.

#### Linux (Debian/Ubuntu)

Run:

```bash
sudo ./scripts/setup_odbc_linux.sh
```

This installs `unixodbc` + `libsqliteodbc` and registers the driver in `/etc/odbcinst.ini` under the name
`SQLite3 ODBC Driver`.

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

### Status colors (icons)

- **Green**: up to date / clean
- **Yellow**: local changes or newly submitted requests not yet confirmed
- **Blue**: remote applied your request; sync needed
- **Red**: remote out-of-date vs your local
- **Gray**: unknown/stale (needs Fetch remote)
