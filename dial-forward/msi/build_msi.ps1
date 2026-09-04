Param(
    [string]$Bundle = "pyinstaller\dist\DialForward",
    [string]$Artifact = "DialForward.msi",
    [string]$ProductVersion = "1.2.0"
)
# ============================================================
#  Dial Forward — MSI build (run in GitHub Actions, Windows)
#  heat (harvest PyInstaller bundle) -> candle -> light  => MSI
# ============================================================
$ErrorActionPreference = "Stop"

$Wix = Join-Path $env:TEMP "wix311"
if (-not (Test-Path "$Wix\heat.exe")) {
    Write-Host "Downloading WiX Toolset v3.11..."
    Invoke-WebRequest "https://github.com/wixtoolset/wix3/releases/download/wix3112rtm/wix311-binaries.zip" `
        -OutFile "$env:TEMP\wix311.zip"
    Expand-Archive "$env:TEMP\wix311.zip" -DestinationPath $Wix -Force
}
$Heat  = "$Wix\heat.exe"
$Candle= "$Wix\candle.exe"
$Light = "$Wix\light.exe"

# --- 1. harvest the bundle folder (files land directly in INSTALLDIR) ---
Write-Host "Heat: harvesting $Bundle ..."
& $Heat dir "$Bundle" -nologo -cg AppComponents -gg -srd -sreg -sfrag `
    -dr INSTALLDIR -var var.BundleDir -out bundle.wxs
if ($LASTEXITCODE -ne 0) { throw "heat failed" }

# --- 2. product template ---
$Template = @"
<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="Dial Forward" Language="1033" Version="$ProductVersion"
           Manufacturer="Dial Forward"
           UpgradeCode="D1A1F000-0000-4000-9000-A1F0D1A1F000">
    <Package InstallerVersion="500" Compressed="yes" InstallScope="perUser"
             Description="Dial Forward - P2P calls over Telegram"
             Manufacturer="Dial Forward" />
    <MajorUpgrade DowngradeErrorMessage="A newer version is already installed." />
    <Media Id="1" Cabinet="data.cab" EmbedCab="yes" />
    <Property Id="ARPPRODUCTICON" Value="DF.ICO" />
    <Icon Id="DF.ICO" SourceFile="icons\dial_forward.ico" />
    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="LocalAppDataFolder">
        <Directory Id="INSTALLDIR" Name="DialForward" />
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Directory Id="AppMenuDir" Name="Dial Forward">
          <Component Id="C_Menu" Guid="A1B2C3D4-0000-4000-8000-000000000002" DiskId="1">
            <Shortcut Id="S_Main" Name="Dial Forward"
                      Target="[INSTALLDIR]DialForward.exe"
                      WorkingDirectory="INSTALLDIR" Icon="DF.ICO"
                      Description="Launch Dial Forward" />
            <RegistryValue Root="HKCU" Key="Software\DialForward" Name="installed"
                           Type="integer" Value="1" KeyPath="yes" />
            <RemoveFolder Id="R_AppMenuDir" On="uninstall" />
          </Component>
        </Directory>
      </Directory>
    </Directory>
    <Feature Id="Main" Title="Dial Forward" Level="1">
      <ComponentGroupRef Id="AppComponents" />
      <ComponentRef Id="C_Menu" />
    </Feature>
    <UIRef Id="WixUI_Minimal" />
  </Product>
</Wix>
"@
Set-Content -Path "main.wxs" -Value $Template -Encoding UTF8

# --- 3. compile & link ---
Write-Host "Candle: compiling..."
$candleArgs = @("-nologo", "-dBundleDir=$Bundle", "main.wxs", "bundle.wxs")
& $Candle @candleArgs
if ($LASTEXITCODE -ne 0) { throw "candle failed" }

Write-Host "Light: linking -> $Artifact"
$lightArgs = @("-nologo", "-ext", "WixUIExtension", "-sice:ICE38",
               "-sice:ICE64", "-sice:ICE91", "-sw1076", "-o", $Artifact,
               "main.wixobj", "bundle.wixobj")
& $Light @lightArgs
if ($LASTEXITCODE -ne 0) { throw "light failed: $LASTEXITCODE" }

Write-Host "MSI built: $Artifact"
