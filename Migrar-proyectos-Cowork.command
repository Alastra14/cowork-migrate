#!/bin/bash
# Doble clic para abrir el asistente de migración de proyectos de Claude Cowork.
cd "$(dirname "$0")" || exit 1
clear
echo "Iniciando el asistente de migración de Cowork…"
echo "(Si Claude Desktop está abierto, ciérralo con Cmd+Q antes de continuar.)"
echo
python3 cowork_migrate.py wizard
echo
echo "Listo. Puedes cerrar esta ventana."
read -r -p "Presiona Enter para salir…" _
