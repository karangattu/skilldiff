from shiny import App, module, reactive, render, ui


@module.ui
def card_ui():
    return ui.card(
        ui.input_action_button("inc", "Increment"),
        ui.output_text("val"),
    )

@module.server
def card_server(input, output, session):
    tally = reactive.Value(0)

    @reactive.effect
    @reactive.event(input.inc)
    def _():
        tally.set(tally.get() + 1)

    @render.text
    def val():
        return f"{tally()}"

app_ui = ui.page_fluid(
    card_ui("alpha"),
    card_ui("beta"),
)

def server(input, output, session):
    for mod_id in ("alpha", "beta"):
        card_server(mod_id)

app = App(app_ui, server)
