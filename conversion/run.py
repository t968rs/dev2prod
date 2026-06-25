

if __name__ == "__main__":
    from conversion.prod_deploy import load_project, ProdCopier

    cfg, paths = load_project("fpm")
    print(str(cfg))

    # Preview: walk and filter without writing anything.
    preview = ProdCopier(cfg, paths, dry_run=True).run()
    assert preview.ok, preview.errors
    print(f"{len(preview.copied)} files would be copied.")

    # Real deployment (writes to dest_root).
    result = ProdCopier(cfg, paths, dry_run=False).run()
    assert result.ok, result.errors

    print(f"{len(result.copied)} files copied.")
    for sk in result.skipped:
        print(f"\tSkipped: {sk}")
    print("Success!")
