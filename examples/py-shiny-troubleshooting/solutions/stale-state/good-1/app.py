from shiny import App, reactive, render, ui

app_ui = ui.page_fluid(
    ui.input_text("item_name", "Item"),
    ui.input_action_button("add_btn", "Add item"),
    ui.output_text("count_txt"),
)

def server(input, output, session):
    items = reactive.value([])

    @reactive.effect
    @reactive.event(input.add_btn)
    def _():
        items.set([*items(), input.item_name()])

    @render.text
    def count_txt():
        return f"Count: {len(items())}"

app = App(app_ui, server)
