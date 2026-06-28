@echo off
setlocal

set ZIP_NAME=novel_disassembler_release.zip
set TEMP_DIR=temp_pack_dir

echo [1/4] Cleaning old files...
if exist %TEMP_DIR% rmdir /s /q %TEMP_DIR%
if exist %ZIP_NAME% del /q %ZIP_NAME%

echo [2/4] Copying files...
mkdir %TEMP_DIR%
xcopy prompts %TEMP_DIR%\prompts\ /e /i /h /y /q >nul
xcopy references %TEMP_DIR%\references\ /e /i /h /y /q >nul
xcopy schemas %TEMP_DIR%\schemas\ /e /i /h /y /q >nul
xcopy scripts %TEMP_DIR%\scripts\ /e /i /h /y /q >nul
xcopy tools %TEMP_DIR%\tools\ /e /i /h /y /q >nul
copy requirements.txt %TEMP_DIR%\ >nul
copy SKILL.md %TEMP_DIR%\ >nul

echo [3/4] Removing pycache and pyc...
for /d /r %TEMP_DIR% %%d in (__pycache__) do (
    if exist "%%d" rmdir /s /q "%%d"
)
del /s /q %TEMP_DIR%\*.pyc >nul 2>&1

echo [4/4] Zipping files...
powershell -Command "Compress-Archive -Path '%TEMP_DIR%\*' -DestinationPath '%ZIP_NAME%' -Force"

echo Cleaning temp directory...
rmdir /s /q %TEMP_DIR%

echo Done! Created %ZIP_NAME%
pause
