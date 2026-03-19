

if __name__ == "__main__":
    from conversion.prod_deploy import ProdConfig, ProdCopier

    cfg = ProdConfig.from_json("prod_deploy.json")

    # Preview
    result = ProdCopier(cfg, dry_run=True).run()
    print(f"{len(result.copied)} files would be copied")

    # Real run, clean slate
    result = ProdCopier(cfg, clean_dest=True).run()
    assert result.ok, result.errors
