"""Download raw repository files into <id>/raw/ and write SHA256SUMS. Re-runnable (skips existing)."""
import hashlib, pathlib, requests, sys

BOREALIS = "https://borealisdata.ca/api/access/datafile/{}?format=original"
# Dryad API downloads now require auth; Dryad mirrors every dataset on Zenodo.
ZEN = "https://zenodo.org/api/records/{}/files/{}/content"
def zen(rec, *names): return [(ZEN.format(rec, n), n) for n in names]

FILES = {
    "malvil1": zen(3897361, "batch_4.maxmaf.pruned.whitelist3.maxmiss97.recode.vcf", "Environmental_data.csv", "POP.xlsx"),
    "picmar2": zen(19961100, *["BlackSpruceSNPs_FILTERED.csv", "BlackSpruceSNPs_FILTERED_metadata.tsv",
                                                "BlackSpruceSNPs_RAW_metadata.tsv", "BlackSpruce_SNPs_subset.vcf.gz",
                                                "BlackSpruce_SNPs_subset_metadata.tsv"]),
    "prusal1": [(BOREALIS.format(i), f) for i, f in [(1054892, "100A_README.txt"), (955435, "Code_Names.txt"),
                                                     (955439, "Genotype_Matrix.txt"), (955438, "Postimputation_SNPs.vcf")]],
    "anogla1": [(BOREALIS.format(i), f) for i, f in [(331553, "Agla_359_6102.vcf"), (331551, "Agla_37_6102.vcf"),
                                                     (331554, "README.txt"), (331550, "dryad_866t1g1rb.json"),
                                                     (331552, "sample_list.txt")]],
    "salfon1": zen(3964857, "50_pop_without_outliers_ind_filtered_4_70_0_2.singleton.unlinked_0.5.1M.vcf"),
    "homame1": zen(3997810, "filtered_singleton_SNPs.vcf", "Sea_surface_temperatures.txt", "SNPs_caracterization_metrics.txt",
                   "00_classify_snps_lobster_Rapture.R"),
    "picgla2": zen(5000066, "README_for_S1.docx", "S1.txt", "S2.txt"),
    "picsit1": zen(10537092, "README.md", "SNPChip_Genotype_Table.txt", "SNPChip_Pedigree_vertical.csv", "SNPChip_Linkage_Map.csv",
                   "SNP_Chip_Data.csv", "RADChip_Genotype_Table.txt", "RADChip_Pedigree_vertical.csv", "RADChip_Map_Info.csv",
                   "RADChip_Linkage_Map.csv", "RADSeq_Genotype_Table.txt", "RADSeq_Pedigree_vertical.csv", "RADSeq_Linkage_Map.csv",
                   "Integrated_Map_White_Sitka.csv") + zen(8274591, "SNPtable_to_VCF.sh", "create_LepMap_files.R"),
    "triaes1": [(BOREALIS.format(i), f) for i, f in [(885571, "B1_README_Genotypic_data_2.txt"), (885517, "B2_Genotypic_data_2.tab"),
                                                     (885573, "D1_README_phenotypic_data.txt"), (885515, "D2_phenotypic_data.tab"),
                                                     (894802, "C1_README_Genotypic_GAPIT1.txt"), (894803, "C2_Genotypic_GAPIT1.tab"),
                                                     (885570, "A1_README_Gene_function.txt")]],
}

# Lot 2+: FILES of each prep/<id>.py module
import importlib.util
for _f in sorted(pathlib.Path(__file__).resolve().parent.joinpath("prep").glob("[a-z]*.py")):
    _spec = importlib.util.spec_from_file_location(f"prep_{_f.stem}", _f); _m = importlib.util.module_from_spec(_spec)
    try: _spec.loader.exec_module(_m); FILES[_f.stem] = _m.FILES
    except Exception as e: print(f"prep/{_f.name} ignoré : {e}", file=sys.stderr)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

for ds, files in FILES.items():
    if len(sys.argv) > 1 and ds not in sys.argv[1:]: continue
    raw = pathlib.Path(ds, "raw"); raw.mkdir(parents=True, exist_ok=True)
    for url, name in files:
        dest = raw / name
        if dest.exists(): continue
        print(ds, name, flush=True)
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for b in r.iter_content(1 << 20): f.write(b)
                tmp.rename(dest)
        except requests.RequestException as e:
            print(f"FAILED {ds} {name}: {e}", flush=True)
    (raw / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in sorted(raw.iterdir())
                                            if p.is_file() and p.name != "SHA256SUMS" and not p.name.endswith(".part")))
print("done")
