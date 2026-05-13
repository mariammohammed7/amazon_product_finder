@echo off
echo Installing packages (first time only)...
pip install -r requirements.txt >nul 2>&1
echo.
echo Starting PrismFind server...
echo Open your browser at: http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python server.py
pause
