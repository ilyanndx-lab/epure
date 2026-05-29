Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\Users\Ilyan\epure\backend; python -m uvicorn main:app"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\Users\Ilyan\epure\frontend; npm run dev"
Start-Process "http://localhost:5173"
