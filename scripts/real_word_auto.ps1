<#
T30: drive real Microsoft Word through docs/real-word-check.md steps 2-5.

    powershell -ExecutionPolicy Bypass -File scripts\real_word_auto.ps1 C:\t30

Run it after `real_word_check.py build C:\t30` and before `... verify C:\t30`.

Step 2 opens every built document the way a double click does, in a Word instance of its
own (/n), and counts the ones that raise a dialog instead of a document window: that is
"Word found unreadable content". Two controls run first, a broken copy that must raise a
dialog and a good file that must not, so a silent detector cannot report zero.
Steps 3-5 (accept all, reject all, reply, resolve, save) go through Word's automation
interface. Documents you have open in another Word window are left alone.
#>
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$OpenTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
Add-Type @"
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class T30Win {
  delegate bool Proc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(Proc p, IntPtr l);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  /* Visible, titled top-level windows of one process: { class, title, handle }. */
  public static List<string[]> List(uint pid) {
    var r = new List<string[]>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) { var t = new StringBuilder(512); var c = new StringBuilder(256);
        GetWindowText(h, t, 512); GetClassName(h, c, 256);
        if (t.Length > 0) r.Add(new[]{ c.ToString(), t.ToString(), h.ToString() }); }
      return true; }, IntPtr.Zero);
    return r; }
}
"@

$Out = (Resolve-Path $Out).Path
$expected = Join-Path $Out 'expected.json'
if (-not (Test-Path $expected)) { throw "$Out has no expected.json; run the build step first" }
# One built document per top-level key of expected.json (written with a one-space indent).
$built = @(Get-Content $expected -Encoding UTF8 | ForEach-Object { if ($_ -match '^ "(\w+)": ') { "$($Matches[1]).docx" } })
if (-not $built.Count) { throw "no document names read from $expected" }
$WM_CLOSE = 0x10
$MAIN = 'OpusApp'  # class of a Word document window; anything else visible is a dialog
$SETTLE_MS = 2500

$winword = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Winword.exe').'(default)'

function Test-Open([string]$path) {
    $p = Start-Process $winword -ArgumentList '/n', '/q', "`"$path`"" -PassThru
    $leaf = [IO.Path]::GetFileNameWithoutExtension($path)
    $wins = @(); $state = 'timeout'
    $deadline = (Get-Date).AddSeconds($OpenTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if ($p.HasExited) { $state = 'exited'; break }
        $wins = @([T30Win]::List([uint32]$p.Id))
        $dialog = @($wins | Where-Object { $_[0] -ne $MAIN }).Count -gt 0
        $shown = @($wins | Where-Object { $_[0] -eq $MAIN -and $_[1] -like "*$leaf*" }).Count -gt 0
        if (-not ($dialog -or $shown)) { continue }
        # Look again after a pause: a prompt can follow the document window by a moment,
        # and a window that is gone by then (a splash, a notice) was not a prompt.
        Start-Sleep -Milliseconds $SETTLE_MS
        if ($p.HasExited) { $state = 'exited'; break }
        $wins = @([T30Win]::List([uint32]$p.Id))
        $dialog = @($wins | Where-Object { $_[0] -ne $MAIN }).Count -gt 0
        $shown = @($wins | Where-Object { $_[0] -eq $MAIN -and $_[1] -like "*$leaf*" }).Count -gt 0
        if ($dialog) { $state = 'dialog'; break }
        if ($shown) { $state = 'opened'; break }
    }
    $seen = ($wins | ForEach-Object { "[$($_[0])] $($_[1])" }) -join ' ; '
    if (-not $p.HasExited) {
        foreach ($w in @($wins | Where-Object { $_[0] -ne $MAIN })) {
            [void][T30Win]::PostMessage([IntPtr][int64]$w[2], $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
        }
        Start-Sleep -Milliseconds 500
        [void]$p.CloseMainWindow()
        if (-not $p.WaitForExit(8000)) { $p.Kill() }
    }
    Start-Sleep -Milliseconds 500
    return [pscustomobject]@{ Name = (Split-Path $path -Leaf); State = $state; Seen = $seen }
}

function New-BrokenCopy([string]$src, [string]$dst) {
    # A table row with no cell and a paragraph inside a paragraph: Word will not open it as is.
    [IO.File]::Copy($src, $dst, $true)
    $zip = [IO.Compression.ZipFile]::Open($dst, 'Update')
    try {
        $entry = $zip.GetEntry('word/document.xml')
        $reader = New-Object IO.StreamReader($entry.Open()); $xml = $reader.ReadToEnd(); $reader.Dispose()
        $entry.Delete()
        $bad = '<w:body><w:tbl><w:tr></w:tr></w:tbl><w:p><w:p><w:r><w:t>nested</w:t></w:r></w:p></w:p>'
        $writer = New-Object IO.StreamWriter($zip.CreateEntry('word/document.xml').Open())
        $writer.Write($xml.Replace('<w:body>', $bad)); $writer.Dispose()
    } finally { $zip.Dispose() }
}

# Late-bound call: PowerShell's COM binder is inconsistent about [ref] on these methods.
function Invoke-Com($obj, [string]$name, [object[]]$arguments) {
    # Unwrapped: a PSObject around an argument reaches COM as a type mismatch.
    [object[]]$plain = foreach ($a in $arguments) { , $a.PSObject.BaseObject }
    return $obj.GetType().InvokeMember($name, 'InvokeMethod', $null, $obj, $plain)
}

"== controls"
# Next to the documents under test, so both open under the same trust rules.
$control = Join-Path $Out ("t30-control-{0}.docx" -f [guid]::NewGuid().ToString('N'))
try {
    New-BrokenCopy (Join-Path $Out 'plain.docx') $control
    $broken = Test-Open $control
    $good = Test-Open (Join-Path $Out 'plain.docx')
    "broken copy: $($broken.State) -> $($broken.Seen)"
    "plain.docx: $($good.State)"
    if ($broken.State -ne 'dialog' -or $good.State -ne 'opened') {
        throw "the repair-prompt detector does not work here (broken: $($broken.Seen); good: $($good.Seen))"
    }
} finally { if (Test-Path $control) { [IO.File]::Delete($control) } }

"== step 2: open every document"
$prompts = 0
foreach ($name in $built | Sort-Object) {
    $path = Join-Path $Out $name
    if (-not (Test-Path $path)) { throw "$name is named in expected.json and missing from $Out" }
    $r = Test-Open $path
    if ($r.State -ne 'opened') { $prompts++ }
    "{0}: {1} -> {2}" -f $r.Name, $r.State, $r.Seen
}
"opened $($built.Count) documents"

function Open-Doc([string]$name) {
    # No conversion prompt, not read-only, not added to the recent files.
    return Invoke-Com $word.Documents 'Open' @((Join-Path $Out $name), $false, $false, $false)
}
function Save-As($doc, [string]$name) {
    Invoke-Com $doc 'SaveAs2' @((Join-Path $Out $name), 12) | Out-Null  # 12 = wdFormatXMLDocument
    Invoke-Com $doc 'Close' @(0) | Out-Null
}
$word = $null
try {
    # Hidden, so a document the user opens meanwhile does not land in this instance.
    $word = New-Object -ComObject Word.Application
    $word.DisplayAlerts = 0
    "== Word $($word.Version), build $($word.Build)"

    "== step 3: edits.docx, accept all"
    $doc = Open-Doc 'edits.docx'
    foreach ($r in $doc.Revisions) {
        $text = $r.Range.Text -replace '[\r\n\a]', '|'
        if ($text.Length -gt 40) { $text = $text.Substring(0, 40) + '...' }
        "revision type={0} author='{1}' paragraphs={2} text='{3}'" -f $r.Type, $r.Author, $r.Range.Paragraphs.Count, $text
    }
    $doc.AcceptAllRevisions()
    Save-As $doc 'edits-accepted.docx'

    "== step 4: edits.docx, reject all"
    $doc = Open-Doc 'edits.docx'
    $doc.RejectAllRevisions()
    Save-As $doc 'edits-rejected.docx'

    "== step 5: comments.docx, reply and resolve"
    $doc = Open-Doc 'comments.docx'
    foreach ($c in @($doc.Comments)) {
        $body = $c.Range.Text
        if ($body.StartsWith('First')) { Invoke-Com $c.Replies 'Add' @($c.Scope, 'Reply written in Word.') | Out-Null }
        if ($body.StartsWith('Second')) { $c.Done = $true }
    }
    foreach ($c in $doc.Comments) {
        "comment author='{0}' done={1} replies={2} text='{3}'" -f $c.Author, $c.Done, $c.Replies.Count, $c.Range.Text
    }
    Save-As $doc 'comments-word.docx'
} finally {
    if ($word) {
        Invoke-Com $word 'Quit' @(0) | Out-Null
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
    }
}

"real_word_repair_prompts == $prompts"
if ($prompts) { exit 1 }
