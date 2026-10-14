@echo off
echo ===== 启动后端 FastAPI =====
start "Backend Service" cmd /k "cd backend && python main.py"

echo ===== 等待2秒，启动前端 =====
timeout /t 2 /nobreak >nul

start "Frontend" cmd /k "cd frontend && npm run dev"
echo 全部启动完成！
