from shiny import App, module, reactive, render, ui


@module.ui
def counter_ui():
    return ui.card(
        ui.input_action_button("inc", "Increment"),
        ui.output_text("val"),
    )

@module.server
def counter_server(input, output, session):
    count = reactive.value(0)

    @reactive.effect
    @reactive.event(input.inc)
    def _():
        count.set(count() + 1)

    @render.text
    def val():
        return str(count())

app_ui = ui.page_fluid(
    counter_ui("alpha"),
    counter_ui("beta"),
)

def server(input, output, session):
    counter_server("alpha")
    counter_server("beta")

app = App(app_ui, server)
