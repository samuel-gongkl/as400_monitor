# Prevent Screen Saver by simulating a key press at regular intervals
# Press Ctrl+C in the terminal to stop

Add-Type -AssemblyName System.Windows.Forms

Write-Host "Screen saver prevention is running. Press Ctrl+C to stop."

while ($true) {
    # Send the F15 key (invisible, won't interfere with your work)
    [System.Windows.Forms.SendKeys]::SendWait("{F15}")
    Start-Sleep -Seconds 60
}
