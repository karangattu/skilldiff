from shiny import App, reactive, render, ui

def counter_card():
    return ui.card(
        ui.input_action_button("inc", "Increment"),
        ui.output_text("val"),
    )

app_ui = ui.page_fluid(
    counter_card(),
    counter_card(),
)

def server(input, output, session):
    count = reactive.value(0)

    @reactive.effect
    @reactive.event(input.inc)
    def _():
        count.set(count() + 1)

    @render.text
    def val():
        return str(count())

app = App(app_ui, server)
