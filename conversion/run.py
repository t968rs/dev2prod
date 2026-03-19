

if __name__ == "__main__":
    from conversion.prod_deploy import load_project, ProdCopier

    cfg, paths = load_project("fbs")

    # Preview
    result = ProdCopier(cfg, paths, dry_run=True).run()
    print(f"{len(result.copied)} files would be copied")

    # Real run, clean slate
    result = ProdCopier(cfg, clean_dest=True).run()
    assert result.ok, result.errors
