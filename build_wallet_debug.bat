@echo off
setlocal
cd /d "%~dp0"
python build_wallet.py --debug --install-build-deps
endlocal
