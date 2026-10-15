from shiny import App, reactive, render, ui


def counter_card(label: str):
    return ui.card(
        ui.input_action_button("inc", label),
        ui.output_text("val"),
    )

app_ui = ui.page_fluid(
    counter_card("alpha"),
    counter_card("beta"),
)

def server(input, output, session):
    count = reactive.value(0)

    @render.text
    def val():
        return str(count())

app = App(app_ui, server)
