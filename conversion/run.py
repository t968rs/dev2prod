

if __name__ == "__main__":
    from conversion.prod_deploy import load_project, ProdCopier

    cfg, paths = load_project("fpm")
    print(str(cfg))

    # Preview
    result = ProdCopier(cfg, paths, dry_run=False).run()

    print(f"{len(result.copied)} files copied.")
    assert result.ok, result.errors

    if result.ok:
        for sk in result.skipped:
            print(f"\tSkipped: {sk}")
        print("Success!")
