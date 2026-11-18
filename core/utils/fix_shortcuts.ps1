# Keeps ListPilot desktop shortcuts working. Run by Start_ListPilot.bat on every launch.
#
# - Shortcuts that point to the old Start_Kitkat.bat are pointed to Start_ListPilot.bat
#   and renamed "ListPilot".
# - Shortcuts to a ListPilot launcher in a folder that no longer exists (the folder
#   was renamed or moved) are pointed to this folder.
# - The first time, a "ListPilot" shortcut is created when none works yet.
# Shortcuts to other programs, or to another existing ListPilot folder, are not touched.
# Never fails the launcher: every error is ignored.
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string[]]$Desktops = @()
)

try {
    $Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $launcher = Join-Path $Root 'Start_ListPilot.bat'
    $icon = Join-Path $Root 'gui\assets\listpilot.ico'
    $marker = Join-Path $Root '.listpilot_shortcut'
    $description = 'ListPilot - Post, track and verify every Marketplace listing'
    $launcherNames = @('start_kitkat.bat', 'start_listpilot.bat', 'start automation.bat')
    $shell = New-Object -ComObject WScript.Shell

    if ($Desktops.Count -eq 0) {
        $Desktops = @(
            [Environment]::GetFolderPath('Desktop'),
            [Environment]::GetFolderPath('CommonDesktopDirectory')
        )
    }
    $Desktops = @($Desktops | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)

    $working = $false
    foreach ($dir in $Desktops) {
        foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
            try {
                $link = $shell.CreateShortcut($file.FullName)
                $target = [string]$link.TargetPath
                if (-not $target) { continue }
                $leaf = [IO.Path]::GetFileName($target).ToLower()
                if ($launcherNames -notcontains $leaf) { continue }

                $isOldName = ($leaf -eq 'start_kitkat.bat')
                $targetMissing = -not (Test-Path -LiteralPath $target)
                $sameFolder = ([IO.Path]::GetDirectoryName($target).TrimEnd('\') -ieq $Root)
                if (-not $targetMissing -and -not ($isOldName -and $sameFolder)) {
                    # Refresh our existing shortcuts, but never brand another installation.
                    if ($sameFolder -and (Test-Path -LiteralPath $icon) -and
                        $link.IconLocation -ne "$icon,0") {
                        $link.IconLocation = "$icon,0"
                        $link.Save()
                    }
                    $working = $true   # a working shortcut (here or to another install): leave it
                    continue
                }

                $link.TargetPath = $launcher
                $link.WorkingDirectory = $Root
                $link.Description = $description
                if (Test-Path -LiteralPath $icon) { $link.IconLocation = "$icon,0" }
                $link.Save()
                $working = $true

                if ($file.BaseName -match 'kitkat') {
                    $newName = 'ListPilot.lnk'
                    if (Test-Path -LiteralPath (Join-Path $dir $newName)) { $newName = 'ListPilot (updated).lnk' }
                    if (-not (Test-Path -LiteralPath (Join-Path $dir $newName))) {
                        Rename-Item -LiteralPath $file.FullName -NewName $newName
                    }
                }
                Write-Output "Desktop shortcut updated: $($file.BaseName) -> $launcher"
            } catch {
                # e.g. no permission on the shared Public desktop: skip this shortcut
            }
        }
    }

    if (-not $working -and -not (Test-Path -LiteralPath $marker) -and $Desktops.Count -gt 0) {
        $path = Join-Path $Desktops[0] 'ListPilot.lnk'
        if (-not (Test-Path -LiteralPath $path)) {
            $link = $shell.CreateShortcut($path)
            $link.TargetPath = $launcher
            $link.WorkingDirectory = $Root
            $link.Description = $description
            if (Test-Path -LiteralPath $icon) { $link.IconLocation = "$icon,0" }
            $link.Save()
            Write-Output "Desktop shortcut created: $path"
        }
    }
    # Created once only, so a shortcut the user deletes is not brought back.
    Set-Content -LiteralPath $marker -Value 'shortcut checked' -ErrorAction SilentlyContinue
} catch {
    exit 0
}
exit 0
