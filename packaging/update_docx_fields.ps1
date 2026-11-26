<#
  update_docx_fields.ps1 -- let Microsoft Word fill in the table of contents and page numbers
  of a generated .docx, save it, and optionally export a PDF.

  md_to_docx.py writes the TOC as a field Word must compute; without this step the reader is
  asked to "update fields" on first open. Needs Word installed; build_installer.bat skips it
  (with a note) when Word is not available, and the .docx still works -- it just asks.

    powershell -ExecutionPolicy Bypass -File packaging\update_docx_fields.ps1 USER_MANUAL.docx [out.pdf]
#>
param(
    [Parameter(Mandatory = $true)][string]$Docx,
    [string]$Pdf = ""
)
$ErrorActionPreference = "Stop"
$Docx = (Resolve-Path $Docx).Path

$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
    # Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles)
    $doc = $word.Documents.Open($Docx, $false, $false, $false)
    foreach ($toc in $doc.TablesOfContents) { $toc.Update() }
    [void]$doc.Fields.Update()
    foreach ($toc in $doc.TablesOfContents) { $toc.UpdatePageNumbers() }
    $doc.Save()
    if ($Pdf) {
        $Pdf = [IO.Path]::GetFullPath($Pdf)
        $doc.ExportAsFixedFormat($Pdf, 17)      # 17 = wdExportFormatPDF
        Write-Output "exported $Pdf"
    }
    Write-Output "updated fields in $Docx ($($doc.ComputeStatistics(2)) pages)"   # 2 = wdStatisticPages
    $doc.Close($false)
}
finally {
    $word.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
}
