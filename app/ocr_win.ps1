# FotoArchiv – Texterkennung mit der in Windows eingebauten OCR (Windows.Media.Ocr, offline).
# Liest Bildpfade zeilenweise von stdin, schreibt je Bild eine JSON-Zeile {"path","text"} bzw. {"path","error"}.
# Aufruf: powershell -NoProfile -ExecutionPolicy Bypass -File ocr_win.ps1 [Sprache]
param([string]$Lang = "de-DE")
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Console]::InputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
$max = [Math]::Min(2600, [Windows.Media.Ocr.OcrEngine]::MaxImageDimension)  # größer bringt kaum mehr, kostet viel Zeit
while ($null -ne ($p = [Console]::In.ReadLine())) {
    if (-not $p) { continue }
    try {
        $f = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
        $s = Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $best = ""; $bestN = -1
        # erst ungedreht; bei wenig Text die anderen Drehungen (fotografierte Ausweise, Querformat-Scans …)
        foreach ($rot in @("None", "Clockwise90Degrees", "Clockwise270Degrees", "Clockwise180Degrees")) {
            $w = $dec.OrientedPixelWidth; $h = $dec.OrientedPixelHeight
            $tr = [Windows.Graphics.Imaging.BitmapTransform]::new()
            $scale = [Math]::Min(1.0, $max / [Math]::Max($w, $h))
            if ($scale -lt 1.0) { $tr.ScaledWidth = [uint32]($w * $scale); $tr.ScaledHeight = [uint32]($h * $scale) }
            $tr.Rotation = [Windows.Graphics.Imaging.BitmapRotation]::$rot
            $bmp = Await ($dec.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied, $tr,
                [Windows.Graphics.Imaging.ExifOrientationMode]::RespectExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
            $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
            $bmp.Dispose()
            $t = ($res.Lines | ForEach-Object { $_.Text }) -join "`n"
            $n = @($res.Lines | ForEach-Object { $_.Words } | Where-Object { $_.Text.Length -ge 3 }).Count
            if ($n -gt $bestN) { $best = $t; $bestN = $n }
            if ($rot -eq "None" -and $bestN -ge 8) { break }  # nur bei kaum Text alle Drehungen, die beste zählt
        }
        $text = $best
        $s.Dispose()
        [Console]::Out.WriteLine((@{ path = $p; text = $text } | ConvertTo-Json -Compress))
    } catch {
        [Console]::Out.WriteLine((@{ path = $p; error = $(if ($_.Exception.InnerException) { $_.Exception.InnerException.InnerException.Message + " | " + $_.Exception.InnerException.Message } else { $_.Exception.Message }) } | ConvertTo-Json -Compress))
    }
    [Console]::Out.Flush()
}
