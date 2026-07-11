from jinja2 import Environment, PackageLoader, StrictUndefined

_TEMPLATE_ENVIRONMENT = Environment(
    loader=PackageLoader("recall_engine"),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_template(template_name: str) -> str:
    return _TEMPLATE_ENVIRONMENT.get_template(template_name).render()
