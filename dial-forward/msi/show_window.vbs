' Dial Forward installer — shows a dark-themed web page in a window
' right when the MSI starts. Installation itself still runs through the MSI.
' Rendered locally via the built-in mshta.exe (no extra runtime needed).
Function ShowWebWindow(session)
    Dim fso, shell, ts, tmp, html, cmd
    Set fso = CreateObject("Scripting.FileSystemObject")
    Set shell = CreateObject("WScript.Shell")
    tmp = fso.GetSpecialFolder(2)
    html = tmp & "\DialForwardInstall.hta"
    Set ts = fso.CreateTextFile(html, True, True)

    ts.WriteLine "<html>"
    ts.WriteLine "<head>"
    ts.WriteLine "<meta http-equiv='x-ua-compatible' content='IE=edge'>"
    ts.WriteLine "<title>Dial Forward</title>"
    ts.WriteLine "<hta:application id='app' caption='yes' border='thick' innerborder='no' maximizebutton='no' minimizebutton='yes' scroll='no' showintaskbar='yes' singleinstance='yes' sysmenu='yes' contextmenu='no'>"
    ts.WriteLine "<style>"
    ts.WriteLine "  html,body{margin:0;padding:0;width:100%;height:100%}"
    ts.WriteLine "  body{font-family:'Segoe UI',Arial,sans-serif;background:#0d0d0d;color:#ffffff}"
    ts.WriteLine "  .wrap{padding:36px 44px 30px 44px}"
    ts.WriteLine "  .logo{font-size:32px;font-weight:700;letter-spacing:1px;color:#f2f2f2}"
    ts.WriteLine "  .bar{height:3px;background:#2a2a2a;margin:20px 0 26px 0}"
    ts.WriteLine "  .step{font-size:24px;font-weight:600;color:#ffffff;margin:0 0 12px 0}"
    ts.WriteLine "  .desc{font-size:14px;line-height:1.6;color:#b3b3b3;margin:0 0 10px 0}"
    ts.WriteLine "  .progress{height:8px;background:#1f1f1f;border:1px solid #2a2a2a;margin-top:30px}"
    ts.WriteLine "  .fill{width:45%;height:100%;background:#6f6f6f}"
    ts.WriteLine "  .foot{margin-top:28px;font-size:12px;color:#808080}"
    ts.WriteLine "</style>"
    ts.WriteLine "</head>"
    ts.WriteLine "<body>"
    ts.WriteLine "  <div class='wrap'>"
    ts.WriteLine "    <div class='logo'>DIAL FORWARD</div>"
    ts.WriteLine "    <div class='bar'></div>"
    ts.WriteLine "    <p class='step'>Installing Dial Forward</p>"
    ts.WriteLine "    <p class='desc'>P2P voice calls over Telegram.<br>A window has opened and the installer is running in the background.</p>"
    ts.WriteLine "    <div class='progress'><div class='fill'></div></div>"
    ts.WriteLine "    <div class='foot'>Dial Forward installer</div>"
    ts.WriteLine "  </div>"
    ts.WriteLine "</body>"
    ts.WriteLine "</html>"

    ts.Close

    cmd = Chr(34) & "C:\Windows\System32\mshta.exe" & Chr(34) & " " & Chr(34) & html & Chr(34)
    shell.Run cmd, 1, False

    ShowWebWindow = 1
End Function
