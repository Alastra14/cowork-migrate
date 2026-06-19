@echo off
REM Doble clic para abrir el asistente de migracion de proyectos de Claude Cowork (Windows).
cd /d "%~dp0"
echo Iniciando el asistente de migracion de Cowork...
echo (Si Claude Desktop esta abierto, cierralo antes de continuar.)
echo.
python cowork_migrate.py wizard
echo.
echo Listo. Puedes cerrar esta ventana.
pause
