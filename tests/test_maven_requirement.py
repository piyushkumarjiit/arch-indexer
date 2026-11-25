from __future__ import annotations

from pathlib import Path

from arch_indexer.maven import (
    MavenRequirement,
    compare_versions,
    detect_maven_requirement,
    parse_maven_version,
    parse_version_ranges,
)

ENFORCER_POM = """\
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.min.version>3.9.0</maven.min.version>
    <maven.max.version>4.0.0</maven.max.version>
  </properties>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-enforcer-plugin</artifactId>
        <executions><execution><goals><goal>enforce</goal></goals></execution></executions>
        <configuration>
          <rules>
            <requireMavenVersion>
              <version>[${maven.min.version},${maven.max.version})</version>
            </requireMavenVersion>
          </rules>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _write(repo: Path, rel_path: str, content: str) -> Path:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_version_comparison_orders_numeric_and_qualifier_parts():
    assert compare_versions("3.8.7", "3.9.0") == -1
    assert compare_versions("3.9", "3.9.0") == 0
    assert compare_versions("3.9.16", "3.9.0") == 1
    assert compare_versions("3.9.0-rc1", "3.9.0") == -1
    assert compare_versions("4.0.0", "4.0.0") == 0


def test_parse_maven_version_reads_mvn_first_line():
    assert parse_maven_version("Apache Maven 3.8.7 (b89d...)") == "3.8.7"
    assert parse_maven_version("Apache Maven 3.9.16") == "3.9.16"
    assert parse_maven_version("3.9.16") == "3.9.16"
    assert parse_maven_version("3.9.0-rc1") == "3.9.0-rc1"
    assert parse_maven_version("available") is None
    assert parse_maven_version("") is None
    assert parse_maven_version("Error: JAVA_HOME is not defined correctly.") is None


def test_parse_version_ranges_supports_brackets_and_bare_minimums():
    closed = parse_version_ranges("[3.9.0,4.0.0)")
    assert len(closed) == 1
    assert closed[0].accepts("3.9.0") is True
    assert closed[0].accepts("3.9.16") is True
    assert closed[0].accepts("4.0.0") is False
    assert closed[0].accepts("3.8.7") is False

    disjoint = parse_version_ranges("(,1.0],[2.0,)")
    assert [version_range.raw for version_range in disjoint] == ["(,1.0]", "[2.0,)"]

    minimum = parse_version_ranges("3.9.0")
    assert len(minimum) == 1
    assert minimum[0].accepts("3.9.1") is True
    assert minimum[0].accepts("3.8.9") is False

    exact = parse_version_ranges("[3.9.16]")
    assert exact[0].accepts("3.9.16") is True
    assert exact[0].accepts("3.9.17") is False


def test_requirement_without_ranges_uses_wrapper_as_minimum():
    requirement = MavenRequirement(wrapper_version="3.9.16")
    assert requirement.accepts("3.9.16") is True
    assert requirement.accepts("3.10.0") is True
    assert requirement.accepts("3.8.7") is False


def test_detects_enforcer_range_with_properties(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", ENFORCER_POM)
    _write(repo, "service/pom.xml", "<project></project>")

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.describe() == "[3.9.0,4.0.0)"
    assert requirement.accepts("3.8.7") is False
    assert requirement.accepts("3.9.16") is True
    assert requirement.sources == ["pom.xml"]


def test_detects_enforcer_range_in_module_pom(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", "<project></project>")
    _write(
        repo,
        "backend/pom.xml",
        "<project><build><plugins><plugin><configuration><rules>"
        "<requireMavenVersion><version>[3.9.0,4.0.0)</version></requireMavenVersion>"
        "</rules></configuration></plugin></plugins></build></project>",
    )

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.accepts("3.8.7") is False
    assert requirement.sources == ["backend/pom.xml"]


def test_detects_wrapper_version_when_no_enforcer_rule(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", "<project></project>")
    _write(
        repo,
        ".mvn/wrapper/maven-wrapper.properties",
        "distributionUrl=https\\://repo.maven.apache.org/maven2/org/apache/maven/"
        "apache-maven/3.9.16/apache-maven-3.9.16-bin.zip\n",
    )

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.wrapper_version == "3.9.16"
    assert requirement.accepts("3.8.7") is False
    assert requirement.accepts("3.9.16") is True
    assert requirement.sources == [".mvn/wrapper/maven-wrapper.properties"]


def test_enforcer_range_wins_over_wrapper_when_both_present(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", ENFORCER_POM)
    _write(
        repo,
        ".mvn/wrapper/maven-wrapper.properties",
        "distributionUrl=https\\://example.invalid/apache-maven-3.9.16-bin.tar.gz\n",
    )

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.ranges
    # The enforcer range is authoritative; a wrapper newer than the range's floor
    # does not make a satisfying host Maven unusable.
    assert requirement.accepts("3.9.0") is True
    assert requirement.wrapper_version == "3.9.16"


def test_no_requirement_returns_none(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(repo, "pom.xml", "<project></project>")

    assert detect_maven_requirement(repo) is None
    assert detect_maven_requirement(tmp_path / "missing") is None
    assert detect_maven_requirement(None) is None


def test_unresolved_property_is_not_treated_as_a_requirement(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(
        repo,
        "pom.xml",
        "<project><build><plugins><plugin><configuration><rules>"
        "<requireMavenVersion><version>[${missing.min},4.0.0)</version>"
        "</requireMavenVersion></rules></configuration></plugin></plugins></build></project>",
    )

    assert detect_maven_requirement(repo) is None


def test_disjoint_ranges_in_one_spec_are_ored_while_separate_rules_are_anded(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(
        repo,
        "pom.xml",
        "<project><build><plugins><plugin><configuration><rules>"
        "<requireMavenVersion><version>[3.8.1,3.8.4),[3.8.5,)</version></requireMavenVersion>"
        "<requireMavenVersion><version>(,4.0.0)</version></requireMavenVersion>"
        "</rules></configuration></plugin></plugins></build></project>",
    )

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.describe() == "[3.8.1,3.8.4),[3.8.5,), (,4.0.0)"
    assert requirement.accepts("3.8.2") is True
    assert requirement.accepts("3.8.4") is False
    assert requirement.accepts("3.9.16") is True
    assert requirement.accepts("4.0.0") is False


def test_xml_comments_in_pom_are_ignored(tmp_path: Path):
    repo = tmp_path / "repo"
    _write(
        repo,
        "pom.xml",
        "<project>"
        "<properties>"
        "<!-- <maven.min>4.0.0</maven.min> -->"
        "<maven.min>3.9.0</maven.min>"
        "</properties>"
        "<build><plugins><plugin><configuration><rules>"
        "<!-- <requireMavenVersion><version>[5.0.0,)</version></requireMavenVersion> -->"
        "<requireMavenVersion><version>[${maven.min},4.0.0)</version></requireMavenVersion>"
        "</rules></configuration></plugin></plugins></build></project>",
    )

    requirement = detect_maven_requirement(repo)

    assert requirement is not None
    assert requirement.describe() == "[3.9.0,4.0.0)"
    assert requirement.accepts("3.9.16") is True
    assert requirement.accepts("3.8.7") is False
