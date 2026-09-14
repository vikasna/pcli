winget install llama.cpp
version: 0.4.0-dev (build 10919, commit d3146f2b5)
built with Clang 20.1.8 for Windows x86_64

"C:\Users\Vikas\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe" `
  -m "E:\.lmstudio\models\lmstudio-community\Mistral-Nemo-Instruct-2407-GGUF\Mistral-Nemo-Instruct-2407-Q4_K_M.gguf" `
  --chat-template-file "C:\...\mistral_nemo_template.jinja" `
  -c 32768 `
  -np 1 `
  -fa on `
  -ctk q8_0 -ctv q8_0 `
  --host 127.0.0.1 --port 8080