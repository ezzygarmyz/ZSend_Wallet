@echo off
setlocal
cd /d "%~dp0"
python build_wallet.py --install-build-deps
endlocal
