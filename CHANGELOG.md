# Changelog

The 1.4.x changes below are pending upstream release. The 1.4.0 development
milestone is included in 1.4.1.

## 1.4.1 — Unreleased

### Added
- Display the plugin version in the main window title, including after library switching and refresh.

## 1.4.0 — Development milestone

### Added
- Support KiCad 10 alongside KiCad 9, including nested library tables and unpacked symbol libraries.
- Separate KiCad 10 settings, pending work, caches and instance locks while preserving KiCad 9 locations.
- Use bundled `wx.svg` for previews when no external SVG converter is available.
- Add compatibility and installed-package regression checks.

### Changed
- Select the launching editor's matching CLI, configuration and project paths.
- Support bundled Python 3.9, pin the tested IPC dependency and cap advertised compatibility at KiCad 10.
- Share library parsing and package building between their callers.
- Allow environment repair to select the KiCad major and Python interpreter.

### Fixed
- Preserve database-scaffold workflows in PCM packages so a packaged installation can initialize a database.
- Accept successful metadata-worker results and invalidate unpacked-library caches when member files change.
- Avoid false local asset changes for Unicode filenames.
- Stop macOS ODBC setup on installation failure and avoid unnecessary privileged writes.

## 1.3.10 — 2026-09-01

### Added
- Add one-shot Linux host setup with an in-app installation prompt.

### Fixed
- Avoid wxGTK bitmap letterbox failures in previews.

## 1.3.9 — 2026-09-01

### Added
- Add optional local processing for library requests.

### Fixed
- Correct BGA footprint naming and pin-1 markers.
- Make Linux plugin-environment setup reliable.

Earlier entries are summarized from the tagged Git history. See
[GitHub releases](https://github.com/nguyen-v/kicad_library_manager/releases)
for older versions.
