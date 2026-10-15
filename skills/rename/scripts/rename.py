# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
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
    if variable is None:
        # No --variable: presume select already left the var of interest.
        if len(data_vars) != 1:
            raise UsageError(f"input has data variables {data_vars}; pass --variable")
        variable = data_vars[0]
    if variable not in data_vars:
        raise UsageError(f"--variable '{variable}' is not a data variable; have {data_vars}")
    # Zarr stores a '/' name as a nested group, so the variable would vanish.
    if not to_name or "/" in to_name:
        raise UsageError(f"--to-name '{to_name}' must be non-empty and contain no '/'")
    if to_name != variable and (to_name in ds.variables or to_name in ds.dims):
        raise UsageError(
            f"--to-name '{to_name}' already names a variable, coordinate, or dimension"
        )
    return ds.rename({variable: to_name})


if __name__ == "__main__":
    rename()
