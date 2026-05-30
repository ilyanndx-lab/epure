taskkill /F /PID (Get-NetTCPConnection -LocalPort 11434 -State Listen).OwningProcess
Start-Sleep -Seconds 2
Start-Process "ollama" -ArgumentList "serve" -Environment @{OLLAMA_GPU_LAYERS="-1"; OLLAMA_KEEP_ALIVE="-1"}
Start-Process "flm" -ArgumentList "serve", "--port", "11435"
Start-Sleep -Seconds 4
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\Users\Ilyan\epure\backend; python -m uvicorn main:app"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd C:\Users\Ilyan\epure\frontend; npm run dev"
Start-Sleep -Seconds 5
Start-Process "http://localhost:5173"