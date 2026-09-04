@echo off
REM Every `bgate` subcommand, without needing `bgate` on PATH.
REM   bgate.cmd doctor
REM   bgate.cmd projects
REM   bgate.cmd connect claude
REM With no arguments it prints the CLI's own help.
python -m bgate_cli.main %*
