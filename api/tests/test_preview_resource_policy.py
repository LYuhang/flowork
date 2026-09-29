from vibecanvas_api.services.preview_resource_policy import (
    diagram_vfs_read_rules,
    html_vfs_read_rules,
    markdown_vfs_read_rules,
    rules_for_root,
)


def test_html_resource_rules_are_exact_and_dynamic_paths_use_static_directory():
    rules = html_vfs_read_rules(
        """
        <img src="/data/images/cover.png?rev=2">
        <a href="/mount/data/report.pdf">Report</a>
        <script>
          const frame = `/data/dataset/images/${index}.png`;
          fetch('/data/labels/current.json');
          fetch('/data/../logs/private.json');
          fetch('https://example.com/public.png');
        </script>
        <div style="background:url(/run/results/chart.svg)"></div>
        """
    )
    assert rules == (
        "/data/dataset/images/",
        "/data/images/cover.png",
        "/data/labels/current.json",
        "/mount/data/report.pdf",
        "/run/results/chart.svg",
    )
    assert rules_for_root(rules, "mount") == ("/mount/data/report.pdf",)


def test_html_resource_rules_reject_traversal_and_non_vfs_urls():
    assert html_vfs_read_rules(
        """
        <img src="/data/../memory/private.txt">
        <img src="https://example.com/x">
        <script>
          fetch(`/data/${userControlledPath}`);
          fetch(`/mount/${userControlledPath}`);
          fetch(`/run/${userControlledPath}`);
        </script>
        """
    ) == ()


def test_markdown_resource_rules_resolve_image_syntax_within_source_root():
    assert markdown_vfs_read_rules(
        "![Architecture](handbook-architecture.svg)\n"
        "![Shared asset](../assets/shared.png?rev=2)\n"
        "![Absolute](/data/brand/logo.svg)\n"
        "![Reference][cover]\n"
        "[cover]: reference-cover.webp\n"
        "[ordinary link](private.txt)\n",
        "/data/handbooks/operations-handbook.md",
    ) == (
        "/data/assets/shared.png",
        "/data/brand/logo.svg",
        "/data/handbooks/handbook-architecture.svg",
        "/data/handbooks/reference-cover.webp",
    )


def test_markdown_resource_rules_reject_external_and_cross_root_images():
    assert markdown_vfs_read_rules(
        """
        ![Remote](https://example.com/tracker.png)
        ![Escape](../../memory/private.png)
        ![Backslash](images\\private.png)
        """,
        "/data/handbooks/operations-handbook.md",
    ) == ()


def test_diagram_resource_rules_only_accept_image_element_vfs_files():
    assert diagram_vfs_read_rules(
        """
        {
          "model": {
            "sceneGraph": {
              "elements": [
                {"elementType": "image", "resourceRef": "/data/images/preview.png"},
                {"elementType": "image", "resourceRef": "/data/../memory/private.png"},
                {"elementType": "image", "resourceRef": "https://example.com/tracker.png"},
                {"elementType": "shape", "resourceRef": "/data/private.txt"},
                {"elementType": "image", "metadata": {"resourceRef": "/logs/private.log"}}
              ]
            }
          },
          "metadata": {"resourceRef": "/memory/private.txt"}
        }
        """
    ) == ("/data/images/preview.png",)


def test_diagram_resource_rules_support_freeform_plan_graphs_and_invalid_json():
    assert diagram_vfs_read_rules(
        b'{"graph":{"elements":[{"elementType":"image",'
        b'"resourceRef":"/mount/brand/logo.svg"}]}}'
    ) == ("/mount/brand/logo.svg",)
    assert diagram_vfs_read_rules(b"not-json") == ()


def test_html_file_relative_images_are_authorized_as_exact_files():
    assert html_vfs_read_rules(
        '<img src="01_lijiang_lake.jpg">'
        '<img src="./02_ayacucho_andes.jpg?rev=2">'
        '<img src="../shared/green%20field.jpg">'
        '<video poster="03_west_java_fields.jpg"></video>'
        '<img src="/mount/brand/logo.png">',
        "/data/landscape_recognition/results_report.html",
    ) == (
        "/data/landscape_recognition/01_lijiang_lake.jpg",
        "/data/landscape_recognition/02_ayacucho_andes.jpg",
        "/data/landscape_recognition/03_west_java_fields.jpg",
        "/data/shared/green field.jpg",
        "/mount/brand/logo.png",
    )


def test_html_file_relative_resources_cannot_expand_root_or_escape_to_other_roots():
    assert html_vfs_read_rules(
        '<img src="../../memory/private.png">'
        '<img src="https://example.com/tracker.png">'
        '<img src="//example.com/tracker.png">'
        '<img src="data:image/png;base64,AA">'
        '<img src="../">'
        '<img src=".">'
        '<img src="images/${name}">'
        '<img src="images%5cprivate.png">',
        "/data/report/index.html",
    ) == ()


def test_inline_html_has_no_implicit_relative_directory():
    assert html_vfs_read_rules('<img src="photo.jpg">') == ()
