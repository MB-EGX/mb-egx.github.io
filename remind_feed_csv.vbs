' remind_feed_csv.vbs
'
' Shows a simple popup reminding you to feed today's CSV/spreadsheet
' files into Stock-Web before the day's scheduled posts run.
'
' This is meant to be triggered by Windows Task Scheduler (see setup
' steps) — it needs no console window and just pops a message box.

Set WshShell = CreateObject("WScript.Shell")
WshShell.Popup "Feed today's CSV files into Stock-Web now, then run launch_and_publish.bat." & vbCrLf & vbCrLf & "(Scheduled posts need today's data before they fire.)", 0, "MB-EGX Reminder", 48
