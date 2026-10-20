"""Presentation-only translation for legacy system-generated Indonesian messages.

Stored application records are not modified. This helper lets an English UI display
known legacy system messages in English while leaving user-entered free text untouched.
"""
from __future__ import annotations
import json
import re

_TRANSLATIONS = {
    'Instruksi berbahaya ditujukan ke AI': 'Malicious instruction directed at the AI',
    'Dokumen berisi teks yang tampak mencoba mengubah perilaku AI atau melewati kontrol keamanan. Konten diperlakukan sebagai data dan tidak diikuti.': 'The document contains text that appears to attempt to change AI behavior or bypass security controls. The content is treated as data and is not followed.',
    'Dokumen tidak boleh mengendalikan kebijakan AI': 'Documents must not control AI policy',
    'Vendor belum terverifikasi': 'Vendor is not verified',
    'Profil vendor ditemukan, tetapi status verifikasinya belum VERIFIED.': 'A vendor profile was found, but its verification status is not VERIFIED.',
    'Nama perusahaan tidak cocok': 'Company name mismatch',
    'Nama perusahaan pada invoice berbeda dari nama vendor tepercaya.': 'The company name on the invoice differs from the trusted vendor record.',
    'Identitas pajak tidak cocok': 'Tax ID mismatch',
    'Nomor identitas pajak berbeda dari profil vendor.': 'The tax ID differs from the vendor profile.',
    'Tujuan pembayaran belum terverifikasi': 'Payment destination is not verified',
    'Rekening pada invoice tidak cocok dengan rekening vendor yang sedang terverifikasi. Rekening ini baru bagi catatan perusahaan, bukan berarti baru dibuka di bank.': 'The account on the invoice does not match the currently verified vendor account. This account is new to the company records; that does not mean it was recently opened at the bank.',
    'Nama pemilik rekening tidak cocok': 'Bank account holder mismatch',
    'Nama pemilik rekening pada invoice berbeda dari rekening vendor yang terverifikasi.': 'The account holder name on the invoice differs from the verified vendor account.',
    'Nomor rekening belum terbaca': 'Bank account number not extracted',
    'Nomor rekening pembayaran kosong atau tidak berhasil diekstrak.': 'The payment account number is missing or could not be extracted.',
    'Nomor rekening terverifikasi': 'Verified bank account number',
    'Vendor tidak ditemukan di database tepercaya': 'Vendor not found in the trusted database',
    'Identitas vendor pada invoice belum cocok dengan profil tepercaya. Jangan menyimpulkan legitimasi hanya dari nama pada dokumen.': 'The vendor identity on the invoice does not match a trusted profile. Do not infer legitimacy from the document name alone.',
    'Vendor terverifikasi': 'Verified vendor',
    'Nomor invoice': 'Invoice number',
    'Nama vendor': 'Vendor name',
    'Total invoice': 'Invoice total',
    'Tanggal jatuh tempo': 'Due date',
    'Data invoice belum lengkap': 'Invoice data is incomplete',
    'Semua field wajib': 'All required fields',
    'Jumlah kuantitas × harga satuan berbeda dari subtotal yang tertulis. Periksa OCR, diskon, dan format pajak.': 'Quantity multiplied by unit price differs from the stated subtotal. Check OCR, discounts, and tax formatting.',
    'Subtotal berbeda dari rincian item': 'Subtotal differs from line items',
    'Kemungkinan invoice duplikat': 'Potential duplicate invoice',
    'Nomor invoice dan vendor cocok dengan invoice lain yang sudah tercatat. Pastikan tagihan tidak dibayar dua kali.': 'The invoice number and vendor match another recorded invoice. Ensure the bill is not paid twice.',
    'Purchase Order tidak ditemukan': 'Purchase Order not found',
    'Nomor PO pada invoice belum cocok dengan database. Personel dapat mengunggah PO atau memilih kecocokan secara manual.': 'The PO number on the invoice does not match the database. Personnel can upload a PO or select a match manually.',
    'Tidak ditemukan': 'Not found',
    'Referensi PO tidak tersedia': 'PO reference is missing',
    'Invoice tidak menyertakan referensi PO. Verifikasi tambahan diperlukan sesuai kebijakan perusahaan.': 'The invoice does not include a PO reference. Additional verification is required under company policy.',
    'Vendor PO tidak cocok': 'PO vendor mismatch',
    'PO yang cocok berdasarkan nomor terdaftar pada vendor berbeda.': 'The PO matched by number is registered to a different vendor.',
    'Total invoice berbeda signifikan dari PO': 'Invoice total differs significantly from the PO',
    'Selisih total melebihi ambang awal (maksimal 10% atau Rp500.000, mana yang lebih besar). Ambang ini adalah aturan demo dan perlu disesuaikan dengan kebijakan perusahaan.': 'This legacy finding used a prior IDR-only threshold. Review the current currency-aware PO threshold and company policy.',
    'Total invoice sedikit berbeda dari PO': 'Invoice total differs slightly from the PO',
    'Ada selisih nilai invoice dan PO yang perlu ditinjau.': 'There is a difference between the invoice and PO amounts that requires review.',
    'Kuantitas item berbeda dari PO': 'Item quantity differs from the PO',
    'Harga satuan item berbeda dari PO': 'Item unit price differs from the PO',
    'Item PO tidak ditemukan pada invoice': 'PO item missing from invoice',
    'Nominal menyimpang dari riwayat': 'Amount deviates from history',
    'Total invoice jauh di luar median invoice historis vendor. Perbedaan bisa sah, tetapi perlu ditinjau.': 'The invoice total is far outside the vendor’s historical median. The difference may be legitimate but requires review.',
    'Ekstraksi belum lengkap': 'Extraction is incomplete',
    'Dokumen belum memiliki hasil ekstraksi yang selesai dan tervalidasi.': 'The document does not yet have a completed and validated extraction result.',
    'Kosong': 'Empty',
    'Semua field wajib': 'All required fields',
    'Dokumen masuk antrean pemrosesan': 'Document added to the processing queue',
    'Analisis data demo tersimpan': 'Demo data analysis saved',
    'Ekstraksi dan pemeriksaan awal selesai': 'Extraction and initial checks completed',
    'Ekstraksi gagal; pemeriksaan manual diperlukan': 'Extraction failed; manual review is required',
    'Dokumen tidak berhasil diproses. Periksa format atau keterbacaan.': 'The document could not be processed. Check its format and readability.',
    'Invoice berhasil diterima dan masuk antrean.': 'Invoice received and added to the queue.',
    'Invoice sintetis dibuat untuk demo lokal': 'Synthetic invoice created for the local demo',
    '50 invoice sintetis dan data pendukung dibuat': 'Created 50 synthetic invoices and supporting data',
    'Personel mengoreksi data hasil ekstraksi.': 'Personnel corrected extracted data.',
    'Risk engine dijalankan ulang setelah koreksi data.': 'The risk engine was rerun after data correction.',
    'Analisis risiko dijalankan ulang atas permintaan personel.': 'Risk analysis was rerun at personnel request.',
    'Personel mencatat hasil pemeriksaan. Catatan ini bukan verifikasi otomatis oleh pihak bank.': 'Personnel recorded the verification outcome. This note is not an automatic verification by the bank.',
    'Personel mengonfirmasi pasangan PO.': 'Personnel confirmed the PO match.',
    'Persetujuan ditolak karena klasifikasi risiko bukan GREEN.': 'Approval was denied because the risk classification is not GREEN.',
    'Invoice berstatus REJECTED dan tidak dapat dibuka kembali melalui persetujuan normal.': 'The invoice is REJECTED and cannot be reopened through standard approval.',
    'Rekening belum terverifikasi.': 'The bank account is not verified.',
    'Personel menyetujui invoice GREEN.': 'Personnel approved the GREEN invoice.',
    'Pembayaran simulasi berhasil; tidak ada transfer uang sungguhan.': 'Simulated payment succeeded; no real money was transferred.',
    'Permintaan resolusi ditolak.': 'Resolution request rejected.',
    'Perubahan rekening ditolak.': 'Bank-account change rejected.',
    'Perubahan rekening disetujui oleh personel kedua setelah verifikasi.': 'Bank-account change approved by a second person after verification.',
    'Jenis temuan RED tidak eligible untuk pengecualian.': 'This type of RED finding is not eligible for an exception.',
    'PO diunggah dan diparsing untuk pencocokan invoice.': 'PO uploaded and parsed for invoice matching.',
    'Pengguna mengekspor laporan audit Excel.': 'User exported the Excel audit report.',
    'Profile vendor baru dibuat. Rekening belum diverifikasi.': 'A new vendor profile was created. The bank account is not verified.',
    'Permintaan resolusi ditolak.': 'Resolution request rejected.',
    'Perubahan rekening ditolak.': 'Bank-account change rejected.',
    'Perubahan rekening disetujui oleh personel kedua setelah verifikasi.': 'Bank-account change approved by a second person after verification.',
    'Pemilik:': 'Account holder:',
    'Kuantitas untuk item': 'Quantity for item',
    'Harga satuan untuk item': 'Unit price for item',
    'ditinjau supervisor': 'reviewed by the supervisor',
    'Dikonfirmasi melalui nomor resmi yang terdaftar untuk uji lokal.': 'Confirmed through the official number registered for the local test.',
    'Konfirmasi dilakukan melalui kanal kontak independen yang didaftarkan untuk simulasi demo.': 'Confirmation was performed through an independently registered contact channel for the demo simulation.',
    'Bukti ditinjau supervisor': 'Evidence reviewed by the supervisor',
    'Bukti amandemen ditinjau': 'Amendment evidence reviewed',
    'PO amandemen telah diperiksa': 'The amended PO has been reviewed',
    'Ada catatan amandemen PO yang dikonfirmasi secara independen untuk demo ini.': 'An amended PO record was independently confirmed for this demo.',
    'Uji alur dual authorization': 'Dual-authorization workflow test',
    'Verifikasi rekening awal vendor': 'Initial vendor bank-account verification',
    'Percobaan': 'Test attempt',
    'Coba bypass YELLOW': 'Attempt to bypass YELLOW',
    'Coba approve RED': 'Attempt to approve RED',
    'Uji persetujuan demo': 'Demo approval test',
    'Jalan Uji 1, Medan': 'Test Street 1, Medan',
    'Hanya supervisor atau admin yang dapat memberikan persetujuan kedua.': 'Only a supervisor or administrator can provide the second approval.',
}

_PREFIX_TRANSLATIONS = (
    (re.compile(r'^DATA DEMO\s*[—-]\s*', re.I), 'DEMO DATA — '),
)
_PATTERN_TRANSLATIONS = (
    (re.compile(r'^Kuantitas untuk item (.+) berbeda dari PO\.$'), r'Quantity for item \1 differs from the PO.'),
    (re.compile(r'^Harga satuan untuk item (.+) berbeda secara material dari PO\.$'), r'The unit price for item \1 differs materially from the PO.'),
    (re.compile(r'^Item (.+) pada PO tidak ditemukan pada rincian invoice\.$'), r'PO item \1 was not found in the invoice line items.'),
    (re.compile(r'^Total invoice berbeda signifikan dari PO$'), 'Invoice total differs significantly from the PO'),
)

def ui_text(value):
    """Translate known application-generated legacy text for display only."""
    if not isinstance(value, str):
        return value
    if value in _TRANSLATIONS:
        return _TRANSLATIONS[value]
    for pattern, replacement in _PREFIX_TRANSLATIONS:
        value, count = pattern.subn(replacement, value, count=1)
        if count:
            return value
    for pattern, replacement in _PATTERN_TRANSLATIONS:
        if pattern.match(value):
            return pattern.sub(replacement, value)
    return value

def translate_json_text(value):
    """Translate narrative strings inside an exported JSON text value, preserving structure."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return ui_text(value)
    def walk(node):
        if isinstance(node, str):
            return ui_text(node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        return node
    return json.dumps(walk(decoded), ensure_ascii=False)
