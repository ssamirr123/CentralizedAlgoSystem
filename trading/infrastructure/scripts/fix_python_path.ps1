# Adds the per-user Python install (found via SSM's earlier file search:
# C:\Users\Administrator\AppData\Local\Programs\Python\Python313) to the
# MACHINE-scope PATH, so SSM's SYSTEM-context PowerShell sessions can see
# it. SSM's RunPowerShellScript runs as SYSTEM, not as the interactive
# Administrator user, so a per-user ("install for me only") Python install
# is invisible to it even though `python --version` works fine over RDP.

$pyDir = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313"

if (-not (Test-Path $pyDir)) {
    Write-Output "ERROR: $pyDir does not exist — re-check the install location."
    exit 1
}

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")

if ($machinePath -notlike "*$pyDir*") {
    $newPath = "$machinePath;$pyDir;$pyDir\Scripts"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")
    Write-Output "Added $pyDir to machine PATH."
} else {
    Write-Output "$pyDir already on machine PATH — nothing to do."
}
