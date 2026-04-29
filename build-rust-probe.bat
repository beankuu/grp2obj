@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "RUST_DIR=%SCRIPT_DIR%src\rust"

if not exist "%RUST_DIR%\Cargo.toml" (
  echo Error: src\rust\Cargo.toml not found.
  exit /b 1
)

pushd "%RUST_DIR%"
cargo build --release
if errorlevel 1 (
  popd
  echo Error: cargo build failed.
  exit /b 1
)
popd

echo Built: %RUST_DIR%\target\release\grp_probe.exe
exit /b 0
