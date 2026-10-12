# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
#   "cftime>=1.6",
# ]
# ///
"""Rename one data variable (omit --variable after select left a single var)."""

from weather_skills_core import Dataset, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.2"


@weather_skill(
    name="rename",
    version=_SKILL_VERSION,
)
@weather_skill.argument("-i", "--input", type=Dataset("any"), required=True)
@weather_skill.argument("--variable", "-v")
@weather_skill.argument("--to-name", required=True, help="New variable name.")
def rename(ds, variable, to_name, **kwargs):
    """Rename one data variable."""
    data_vars = list(ds.data_vars)
    if variable:
        if variable not in ds.data_vars:
            raise UsageError(
                f"--variable '{variable}' is not a data variable (have {data_vars}); "
                "coordinates and dimensions are not renamed"
            )
        name = variable
    elif len(data_vars) == 1:
        name = data_vars[0]
    else:
        raise UsageError(f"input has {len(data_vars)} data variables {data_vars}; pass --variable")
    if to_name != name and (to_name in ds.data_vars or to_name in ds.coords or to_name in ds.dims):
        raise UsageError(
            f"--to-name '{to_name}' already names a variable, coordinate, or dimension"
        )
    return ds.rename({name: to_name})


if __name__ == "__main__":
    rename()
