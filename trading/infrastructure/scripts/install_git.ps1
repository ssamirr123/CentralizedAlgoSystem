# Downloads and silently installs Git for Windows (machine-wide, adds
# itself to PATH via its own installer -- unlike the Python installer
# issue, Git for Windows' installer handles this correctly by default).

$installerUrl = "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe"
$installerPath = "C:\git-installer.exe"

Write-Output "Downloading Git installer..."
Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath

Write-Output "Installing Git (silent)..."
Start-Process -FilePath $installerPath -ArgumentList "/VERYSILENT /NORESTART" -Wait

Remove-Item $installerPath -Force

Write-Output "Done."
