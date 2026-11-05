@echo off
rem Show what try_tao.cmd saved in try_tao_out\tao.sqlite.
rem Usage:  view_tao.cmd          (summary of every run + opens HTML report)
rem         view_tao.cmd 8        (one subnet: latest row + price history)
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "DB=try_tao_out\tao.sqlite"
if not exist "%DB%" (echo No %DB% yet. Run try_tao.cmd first. & exit /b 1)

if not "%~1"=="" (
  ".venv\Scripts\tao.exe" --db "%DB%" subnet %1 --history 20
  exit /b %errorlevel%
)

".venv\Scripts\python.exe" -c "import sqlite3; c=sqlite3.connect(r'%DB%'); print('Runs saved in subnet_stats:'); [print('  %%s  %%4d subnets' %% r) for r in c.execute('select fetched_at, count(*) from subnet_stats group by fetched_at order by fetched_at')]; print(); print('Top 10 by market cap (latest run):'); [print('  SN%%-4s %%-24s price %%-10s mcap %%s' %% r) for r in c.execute('select netuid, name, price_tao, market_cap_tao from subnet_stats where fetched_at=(select max(fetched_at) from subnet_stats) order by market_cap_tao desc limit 10')]"

".venv\Scripts\tao.exe" --db "%DB%" report --out try_tao_out\tao_report.html
start "" "try_tao_out\tao_report.html"
