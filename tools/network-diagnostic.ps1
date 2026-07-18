# Soft Clipper - Network Diagnostic
#
# Answers one question: WHY do video downloads fail on this PC?
#   - Your ISP is lying about YouTube's address (DNS block)  -> free fix, no VPN
#   - Your ISP is killing the connection itself (SNI/deep filtering) -> needs proxy/VPN
#   - You reach YouTube fine, but it refuses you (403 / bot check) -> needs cookies
#   - Nothing is blocked -> the problem is elsewhere
#
# RUN THIS WITH YOUR VPN TURNED OFF, otherwise it measures the VPN, not your ISP.

$ErrorActionPreference = 'SilentlyContinue'
$Target = 'www.youtube.com'

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Good($t)    { Write-Host "  $t" -ForegroundColor Green }
function Bad($t)     { Write-Host "  $t" -ForegroundColor Red }
function Info($t)    { Write-Host "  $t" -ForegroundColor Gray }

Write-Host "`nSoft Clipper - Network Diagnostic" -ForegroundColor White
Write-Host "Make sure your VPN is OFF before running this.`n" -ForegroundColor Yellow

# --- 1. What does your ISP's DNS say? ---------------------------------------
Section "1. Your ISP's DNS"
$sysIPs = @()
try {
    $sysIPs = @((Resolve-DnsName -Name $Target -Type A -ErrorAction Stop |
                 Where-Object { $_.IPAddress }).IPAddress)
    if ($sysIPs.Count) { Info "$Target -> $($sysIPs -join ', ')" }
    else { Bad "No A record returned (suspicious)" }
} catch {
    Bad "DNS lookup failed: $($_.Exception.Message)"
}

# --- 2. What does an honest DNS say? ----------------------------------------
# Query Cloudflare over HTTPS by IP, so a poisoned DNS can't interfere.
Section "2. Cloudflare DNS (the honest answer)"
$realIPs = @()
try {
    $resp = Invoke-RestMethod -Uri "https://1.1.1.1/dns-query?name=$Target&type=A" `
            -Headers @{ accept = 'application/dns-json' } -TimeoutSec 15 -ErrorAction Stop
    $realIPs = @(($resp.Answer | Where-Object { $_.type -eq 1 }).data)
    if ($realIPs.Count) { Info "$Target -> $($realIPs -join ', ')" }
    else { Bad "Cloudflare returned no A record" }
} catch {
    Bad "Couldn't reach Cloudflare DNS: $($_.Exception.Message)"
}

# --- 3. Is the DNS answer a lie? --------------------------------------------
Section "3. DNS comparison"
$dnsLies = $false
if ($sysIPs.Count -and $realIPs.Count) {
    $bogus = $sysIPs | Where-Object { $_ -in @('0.0.0.0', '127.0.0.1', '::1') }
    $overlap = $sysIPs | Where-Object { $_ -in $realIPs }
    if ($bogus) {
        $dnsLies = $true
        Bad "Your ISP returns a fake address ($($bogus -join ', ')) -> DNS BLOCK"
    } elseif (-not $overlap) {
        $dnsLies = $true
        Bad "Your ISP's answer doesn't match Cloudflare's -> DNS likely tampered"
    } else {
        Good "ISP DNS agrees with Cloudflare - DNS is not the problem"
    }
} else {
    Info "Not enough data to compare"
}

# --- 4. Can we actually open a TLS connection? ------------------------------
# TCP + TLS handshake straight to the real IP. If TCP connects but the TLS
# handshake dies, the ISP is filtering on SNI (the hostname inside the
# handshake) - and changing DNS will NOT save you.
function Test-Tls($ip, $sni) {
    $tcp = $null; $ssl = $null
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $iar = $tcp.BeginConnect($ip, 443, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(8000)) { return 'TCP_TIMEOUT' }
        $tcp.EndConnect($iar)
        $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, { $true })
        $ssl.AuthenticateAsClient($sni)
        if ($ssl.IsAuthenticated) { return 'OK' } else { return 'TLS_FAILED' }
    } catch {
        return "TLS_BLOCKED: $($_.Exception.Message)"
    } finally {
        if ($ssl) { $ssl.Dispose() }
        if ($tcp) { $tcp.Close() }
    }
}

Section "4. Direct connection to YouTube's real IP"
$tlsResult = 'NO_IP'
if ($realIPs.Count) {
    $ip = $realIPs[0]
    Info "Connecting to $ip (SNI: $Target) ..."
    $tlsResult = Test-Tls $ip $Target
    if ($tlsResult -eq 'OK') { Good "TLS handshake OK - the connection itself is not blocked" }
    else { Bad "TLS handshake failed -> $tlsResult" }

    # Control: same IP, harmless SNI. If this works but YouTube's doesn't,
    # the filtering is definitely keyed on the hostname.
    Info "Control test (same IP, SNI: example.com) ..."
    $ctrl = Test-Tls $ip 'example.com'
    Info "Control result: $ctrl"
} else {
    Bad "No real IP to test against"
}

# --- 5. Does YouTube actually answer? ---------------------------------------
Section "5. HTTPS request to YouTube"
$httpStatus = $null
try {
    $r = Invoke-WebRequest -Uri "https://$Target" -TimeoutSec 20 -UseBasicParsing -ErrorAction Stop
    $httpStatus = $r.StatusCode
    Good "YouTube responded with HTTP $httpStatus"
} catch {
    $httpStatus = $_.Exception.Response.StatusCode.value__
    if ($httpStatus) { Bad "YouTube responded with HTTP $httpStatus" }
    else { Bad "No response: $($_.Exception.Message)" }
}

# --- Verdict ----------------------------------------------------------------
Section "VERDICT"
if ($dnsLies -and $tlsResult -eq 'OK') {
    Write-Host "  DNS BLOCK ONLY - and this is the good news." -ForegroundColor Green
    Write-Host "  Your ISP only lies about YouTube's address. The connection itself works." -ForegroundColor Green
    Write-Host ""
    Write-Host "  FIX (permanent, free, NO VPN):" -ForegroundColor White
    Write-Host "    Change this PC's DNS to Cloudflare:" -ForegroundColor White
    Write-Host "    Settings > Network & Internet > (Wi-Fi or Ethernet) > Hardware properties"
    Write-Host "    > DNS server assignment > Edit > Manual > IPv4 On"
    Write-Host "      Preferred DNS: 1.1.1.1"
    Write-Host "      Alternate DNS: 1.0.0.1"
    Write-Host "    Save, then run this script again to confirm."
    Write-Host ""
    Write-Host "  Bonus: with no VPN your IP stays residential, so YouTube won't 403 you." -ForegroundColor Green
}
elseif ($tlsResult -ne 'OK' -and $tlsResult -ne 'NO_IP') {
    Write-Host "  DEEP FILTERING (SNI-level) - changing DNS will NOT help." -ForegroundColor Yellow
    Write-Host "  Your ISP inspects the hostname and kills the connection." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  FIX:" -ForegroundColor White
    Write-Host "    - Use a VPN, or set a proxy in Soft Clipper's Settings, AND"
    Write-Host "    - Set 'Cookies from browser' in Settings (VPN IPs get 403 without it)."
    Write-Host "    - Best long-term: a residential proxy (no system-wide VPN needed)."
}
elseif ($httpStatus -eq 403) {
    Write-Host "  NOT BLOCKED - but YouTube is refusing you (403)." -ForegroundColor Yellow
    Write-Host "  It thinks you're a bot. This is normal on shared VPN IPs." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  FIX: turn the VPN OFF if you don't need it, and set" -ForegroundColor White
    Write-Host "       'Cookies from browser' (Firefox) in Soft Clipper's Settings."
}
elseif ($httpStatus -eq 200) {
    Write-Host "  NOTHING IS BLOCKED on this PC. YouTube is reachable." -ForegroundColor Green
    Write-Host "  If downloads still fail, it's not your network - send the exact" -ForegroundColor Green
    Write-Host "  error text from Soft Clipper." -ForegroundColor Green
}
else {
    Write-Host "  Inconclusive. Send a screenshot of this whole window." -ForegroundColor Yellow
}

Write-Host ""
Read-Host "Press Enter to close"
