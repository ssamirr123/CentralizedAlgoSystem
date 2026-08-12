# Clones the repo onto the instance at C:\trading-app, on the branch that
# actually has trading/ (main doesn't have it yet -- this branch hasn't
# been merged).

$targetDir = "C:\trading-app"
$repoUrl = "https://github.com/ssamirr123/CentralizedAlgoSystem.git"
$branch = "web-base-algo-trading-control"

if (Test-Path $targetDir) {
    Write-Output "$targetDir already exists -- pulling latest instead of cloning."
    Set-Location $targetDir
    git fetch origin
    git checkout $branch
    git pull origin $branch
} else {
    git clone --branch $branch $repoUrl $targetDir
}

Write-Output "Done."
