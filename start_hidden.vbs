Set WshShell = CreateObject("WScript.Shell")

' Останавливаем ngrok
WshShell.Run "taskkill /im ngrok.exe /f", 0, True

' Останавливаем python бота через PowerShell (ищет по пути freel_bot\.venv)
WshShell.Run "powershell -command ""Get-WmiObject Win32_Process | Where-Object {$_.Name -eq 'python.exe' -and $_.CommandLine -like '*freel_bot*.venv*'} | ForEach-Object {$_.Terminate()}""", 0, True

' Запасной вариант — убить по порту 8081 если вдруг процесс остался
WshShell.Run "powershell -command ""$p = Get-NetTCPConnection -LocalPort 8081 -ErrorAction SilentlyContinue; if($p){Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue}""", 0, True

' Пауза чтобы процессы успели завершиться
WScript.Sleep 3000

' Запуск ngrok скрытно
WshShell.CurrentDirectory = "C:\Users\9BCF~1\Documents\GitHub\freel_bot"
WshShell.Run "C:\Users\9BCF~1\Documents\GitHub\freel_bot\ngrok.exe http 8081", 0, False

' Пауза чтобы ngrok успел запуститься
WScript.Sleep 5000

' Запуск бота скрытно с записью логов в файл
WshShell.CurrentDirectory = "C:\Users\9BCF~1\Documents\GitHub\freel_bot"
WshShell.Run "cmd /c C:\Users\9BCF~1\Documents\GitHub\freel_bot\.venv\Scripts\python.exe -m app >> C:\Users\9BCF~1\Documents\GitHub\freel_bot\bot.log 2>&1", 0, False
