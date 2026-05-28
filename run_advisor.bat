@echo off
cd /d "%~dp0"
echo.
echo  ====================================
echo   SIGNAL ADVISOR — กำลัง scan...
echo  ====================================
echo.
python signal_advisor.py --report
if errorlevel 1 (
  echo.
  echo  [ERROR] เกิดข้อผิดพลาด ดู log ด้านบน
  pause
)
