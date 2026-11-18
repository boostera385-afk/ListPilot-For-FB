@echo off
:: Old launcher name, kept so existing desktop shortcuts keep working.
:: It starts ListPilot, which repoints those shortcuts to Start_ListPilot.bat.
call "%~dp0Start_ListPilot.bat"
