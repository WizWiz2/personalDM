@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PDM_TE2_SEMANTIC_SHADOW_ENABLED=true"
cd /d "%~dp0"
title Personal DM - TE2 Semantic Shadow Contracts

echo =======================================================================
echo          PERSONAL DM - TE2 READ-ONLY SEMANTIC SHADOW SUITE
echo =======================================================================
echo.
echo Legacy Scribe remains the only semantic writer.
echo TE2 extracts read-only residual observations and stores them in turn snapshots.
echo A comparison report is generated after the isolated live-model suite.
echo.

call "%~dp0test-models.bat" %*
set "RC=%ERRORLEVEL%"

echo.
echo [TE2] Building semantic shadow comparison report...
pushd src\backend
python -m live_model_contracts.shadow_report
set "REPORT_RC=%ERRORLEVEL%"
popd

if not "%REPORT_RC%"=="0" (
    echo [WARN] TE2 shadow report could not be generated.
)

set "PDM_TE2_SEMANTIC_SHADOW_ENABLED="
exit /b %RC%
